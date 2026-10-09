# Moss-FinAgent-Research

> **面向二级市场的多 Agent AI 投研系统**。基于 LangGraph StateGraph 的 Supervisor 编排，
> **19 个 Agent 分 5 层**：数据层（A01-A04）/ 信息层（A05-A07）/ 分析层（A08-A12）/
> 行业层（A13-A16）/ 决策层（A17）/ 审计层（A18）/ 工程层（A19）。
> 全链路数据溯源与推理路径可回溯。

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-009688)](https://fastapi.tiangolo.com/)
[![React](https://img.shields.io/badge/React-18-61DAFB)](https://react.dev/)
[![Tests](https://img.shields.io/badge/tests-360%2B%20files%20%C2%B7%2020%2B%20modules-success)](#测试)
[![CI](https://img.shields.io/badge/CI-backend%20%2B%20frontend%20%2B%20compliance-success)](#ci--合规门禁)

---

## 目录

- [它能做什么](#它能做什么)
- [架构总览](#架构总览)
- [三大自研基础设施](#三大自研基础设施)
- [工程纪律](#工程纪律)
- [快速开始](#快速开始)
- [试运行](#试运行)
- [CI / 合规门禁](#ci--合规门禁)
- [已知不足](#已知不足)
- [文档索引](#文档索引)

---

## 它能做什么

| 能力 | 说明 | 主要文件 |
|---|---|---|
| **AI 投研分析** | 输入个股 → Planner 规划 → 数据采集/清洗/校验 → 信息层去伪与事件抽取 → 宏观/中观/微观/风控/合规并行分析 → 行业 Agent → ReAct 综合建议 → 审计封存 | `src/orchestration/` |
| **集合竞价选股**（商业版） | 9:25:00 自动出池，12 维打分 + 10 条否决；规则表 + 位掩码引擎，可一张纸打印、单票可逐条溯源 | `src/auction_select/`、`auction_select/`（不入库） |
| **做 T 辅助** | 分时买卖点、情绪周期研判、权重画像编辑 | `src/intraday/`、`src/domain/intraday/` |
| **量化因子与回测** | 因子库（IC/IR、5 分位分层回测、换手率）、VaR/CVaR、历史压力测试、Brinson 归因、参数敏感性 | `src/quant/`、`src/backtest/` |
| **量化选股 / 日 K 选股**（商业版） | 三档模型定时选股；LightGBM 日 K 多因子选股写入自选股 | `moss_selector/`（不入库） |
| **资金流 / 板块拥挤度** | 大资金动向、概念板块近 6 年拥挤度水位与告警 | `src/fundflow/`、`src/sector_crowding/` |
| **主线挖掘** | 三层漏斗（六维基座→三维建仓痕迹→龙头共振门控）识别板块主线，启动前/初期告警；Walk-Forward 回测 + 监控胜率六章报告；期货先行信号 | `src/mainline/` |
| **ETF 份额监控** | 宽基 ETF 份额申赎的逆周期痕迹：份额环比/5-10-20 日累计 + 指数分位 + 市场环境门控（机会信号仅熊市放行）；多产品共振、行业 ETF 反转警示 | `src/mainline/etf_*`、`configs/etf_flow.yaml` |
| **告警 / 情报** | 事件驱动告警，批量分类 + 批量打分两阶段（2 次 LLM 调用扫 30 条候选）；博查搜索 + 百度千帆两源自动换手 | `src/domain/alerts/`、`src/domain/intel/` |
| **行业轮动日报** | 行业热力图 + 主力流向 + 规则研判 | `src/sector_rotation/` |

---

## 架构总览

### 分层

```
src/
├── api/                         HTTP 接口层（22 主路由 + 2 私有 try/except）
│   ├── routes/                  auth · admin · research · alerts · backtest · fundflow ·
│   │                            intraday · quant · mainline · sector_crowding · sector_rotation ·
│   │                            intel · data · scheduler · metrics · code_engineer · skills · …
│   ├── main.py / runtime.py     FastAPI 装配、依赖注入、事件接线
│   ├── tasks.py / alert_hub.py  异步任务管理、告警广播
│   └── exception_handlers.py / errors.py / error_codes.py
├── orchestration/               LangGraph StateGraph 编排 + Planner
│   ├── supervisor.py            主管图（fan-out / fan-in / ReAct）
│   ├── planner.py               LLM 驱动的指标规划
│   └── message_bus.py
├── domain/                      业务域（**不依赖 infrastructure 具体实现**）
│   ├── agents/
│   │   ├── data/                A01 采集 · A02 清洗 · A03 校验 · A04 存储
│   │   ├── info/                A05 去伪 · A06 抽取 · A07 舆情
│   │   ├── analysis/            A08 宏观 · A09 中观 · A10 微观 · A11 风控 · A12 合规
│   │   ├── industry/            A13 科技 · A14 消费 · A15 周期 · A16 医药（+ 通用兜底）
│   │   ├── decision/            A17 综合建议（ReAct）
│   │   ├── audit/               A18 审计（哈希链，**零 LLM**）
│   │   └── engineering/         A19 代码工程师（自学习数据补充）
│   ├── alerts/ intel/ indicators/ intraday/ quota/ auth/ skills/ platform/
├── core/                        唯一权威层：配置 · 异常 · 取消令牌 · 共享口径
│   ├── base_agent.py / state.py / message.py
│   ├── config.py / models.py / schemas.py
│   ├── market_constants.py      市场口径常量（涨跌停 / 市值 / 年化交易日）
│   ├── trading_session.py       A 股交易时段边界（09:15/09:30/11:30/13/10/11/15/10/15/10）
│   ├── symbols.py               sh/sz 前缀换算（**只此一处**）
│   ├── errors.py / exceptions.py / redaction.py
│   ├── budget.py / cancel.py / inflight.py / loop_lag.py / hop_stats.py
│   ├── logging_setup.py         src 命名空间 HANDLER（防 INFO 被丢弃）
│   └── intel_limits.py          交互路径死线（默认 10s；.env 用 MOSS_QUERY_DEADLINE_SEC）
├── infrastructure/              技术实现
│   ├── llm/                     网关 · 缓存 · 断路器 · 审计 · providers · 速率闸门 · VRAM
│   ├── connectors/              数据源连接器 + 路由（三级故障转移、事件采集器）
│   ├── repositories/            仓储实现（SQLite/Postgres，RLS/审计链/事件/用户池）
│   ├── catalog/                 指标目录 · 频率推断 · 源-列索引 · 同义词 · 联网兜底
│   ├── search/                  博查 / 百度千帆两源
│   ├── notifiers/               邮件等渠道
│   └── security/rls.py          PostgreSQL 原生 RLS 双层隔离
├── quant/                       数据仓库 · 因子 · 回测 · 风控（**核心**）
├── auction_select/              集合竞价选股（商业版，**目录不入库**；本地持有）
├── intraday/                    做 T 辅助（**核心**）
├── backtest/                    短线/单标的回测
├── fundflow/                    资金流监控
├── sector_crowding/             板块拥挤度
├── sector_rotation/             行业轮动日报
├── mainline/                    主线挖掘（三层漏斗 / 期货 / ETF / Walk-Forward）
└── scheduler/                   Celery Beat 注册表 + JSONL 运行记录 + 管理 API

auction_select/                  ← 商业版目录（不入库；本地持有）
moss_selector/                   ← 量化选股（不入库；本地持有）
qmt/                             ← QMT 行情接入（README + SIGNAL_CONTRACT 入库）
configs/                         全局配置（模型路由 / 数据源 / 策略源 / 指标目录）
```

**依赖方向**：`api → orchestration → domain → infrastructure`（单向）。
`domain` 只依赖 `infrastructure` 暴露的**接口**，具体实现由 `api/runtime.py` 注入。

### 一次投研分析的调用链

```mermaid
flowchart LR
    U[用户 query] --> R[POST /api/v1/research/analyze]
    R --> P[Supervisor Planner<br/>本地 light 模型]
    P --> A01[A01 采集<br/>ConnectorRouter 三级故障转移]
    A01 --> A02[A02 清洗 + A03 校验 + A04 入库<br/>纯规则]
    A02 --> A05[A05 去伪 + A06 抽取 + A07 舆情<br/>本地 medium / 云端]
    A05 --> FAN{A08-A12 + 行业 A13-A16<br/>并行 fan-out}
    FAN --> A17[A17 综合建议<br/>ReAct ≤3 步]
    A17 --> A18[A18 审计<br/>哈希链 零 LLM]
    A18 --> Out[返回
```

---

## 三大自研基础设施

### 1 · LLM 网关（`src/infrastructure/llm/`）

**架构红线：所有 LLM 调用必须过网关，禁止 Agent 直连模型 API。**

```
complete(tier, system, prompt)
  ├─ 取消令牌检查      调用前拦截，用户点停止后不再产生费用
  ├─ 输入截断          保头保尾（尾部是指令与 JSON schema）
  ├─ 缓存              精确 + 语义两级，按「模型层级 + json_mode + agent」分区
  ├─ token 预算        单任务累计超限直接拒绝
  ├─ 速率闸门          provider 级滑动窗口
  ├─ 熔断器            provider 级状态机
  └─ 降级链            主模型 → 备模型 → fail-closed，全程落审计
```

配套：**语义缓存**、**prompt/response 哈希审计链**、**按层级的模型路由**
（`local_light=qwen2.5:1.5b-instruct-q4_K_M`、`local_medium=qwen3.5:4b`、
`local_reasoning=deepseek-r1:7b`，详见 `configs/models.yaml`）。

### 2 · 数据连接器路由（`src/infrastructure/connectors/`）

```
分档 TTL 缓存 → per-key lock（防击穿）→ 日线链：AkShare → 腾讯 → Tushare → baostock
                                                  → [本地 CSV 开关 LOCAL_QUOTE_DIR]
                                                  → [QMT 开关 最末，默认关闭]
       分钟/分时/快照链：腾讯 → 新浪 → 东财 → [QMT 开关 最末]
```

- **分档 TTL**：月频宏观 24h / 估值 12h / 行情 4h / 实时流动性 5min。
- **硬约束**：真实源全失败时**绝不回退 simulated**，宁可报缺口。
  假数据进了投研建议，比"这次没数据"危险得多。

### 3 · 自学习数据补充（`A19 DataGapResolverAgent`）

```
指标无数据 → LLM 生成 connector
                ├─ ① AST 静态校验（禁 import os/subprocess、禁 eval）
                └─ ② 子进程沙箱冒烟（15s 超时）
                      ↓ 双通过
                    落库注册，后续复用
```

> ⚠️ 沙箱是**子进程级隔离，不是安全容器**。只适合"半信任"的生成代码在小规模
> 数据源上使用。边界在 `docs/CODE_AUDIT_CHECKLIST.md`。

---

## 工程纪律

| 纪律 | 落地方式 |
|---|---|
| **可复现** | 取数一律按 `trade_date` 钉死，不用"实时时钟"倒推历史 |
| **不猜** | 数据缺失时置 `None` 并记 `degraded`，绝不用 0 或中性值冒充 |
| **可追溯** | 每个数据点带 `source_url / publish_time / fetch_time / raw_content_hash`；LLM 调用带 `prompt_hash / token / 延迟 / 降级链` |
| **白名单驱动** | Agent 数据可见性由 `supervisor._AGENT_DATA_WHITELIST` 严格按 `en_id`/族前缀匹配 |
| **纯重构要有黄金回归** | 集合竞价选股重构：冻结行情录像带逐位比对（47 只 × 12 维）+ 性能门禁单测 |
| **规则可读** | 判定规则收敛成声明式表，`python -m src.auction_select.rulebook --dump` 一张纸打印 |
| **测试** | 360+ 单测文件（`tests/unit`）、10 个集成测试；门禁在 CI 见 §8 |
| **不删别人创建的临时** | `AGENTS.md` "AI 协作护栏" §30 必有：逐个列名字、不写通配符、`git status` 先 |

### 一次真实的重构（架构演进案例）

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
# 1. 依赖（需 Python ≥ 3.10 + uv）
uv sync

# 2. 启动本地数据库（当前阶段用 SQLite 零依赖；PG/Redis 可选）
docker compose up -d

# 3. 环境变量
cp .env.example .env      # 填 DEEPSEEK_API_KEY 等
                            # ⚠️ 外部联网源凭据（bocha_search / baidusearch）
                            # 必须写进项目根 .env，进程从环境变量继承不到

# 4. 启动 API（本机 8100）
$env:PYTHONPATH="."       # bash: export PYTHONPATH=.
uv run uvicorn src.api.main:app --port 8100
# 或用管理脚本（后台 + 日志 + 优雅重启）
uv run python manage.py start --replace --daemon

# 5. 前端（5173）
uv run python manage.py web
# 或一次性构建到 web/dist（后端可同服务托管）
uv run python manage.py build

# 6. 测试（仅运行本次改动相关的测试）
uv run python scripts/affected_tests.py --run
# 全量（仅在提交/推送前）
uv run pytest
```

### 外部联网源（每天源必须先检查）

```bash
# 退出码 0 = 全部可用 / 3 = 有源被拒 / 2 = 探针自己坏了
uv run python scripts/check_external_sources.py
```

| 源 | 角色 | 状态（2026-10-01） |
|---|---|---|
| **博查搜索**（`bocha_search`） | 搜索主源（path C） | ✅ 可用（一次性总量 1000） |
| **百度千帆智能搜索**（`baidusearch`） | 搜索备用 | ✅ 可用（每天 100） |
| **东财 Choice EMQuantAPI** | 数据连接器（**不是搜索源**） | ⛔ 账号未开通「量化接口」权限（`code:160`） |

详见 `docs/PRD.md` §19.33。

---

## 试运行

> ⚠️ **试运行脚本打的是 8100 的 `dev` 实例**（`data/dev/`）。**8110 是对外试点（`pilot`）**、
> 全站强制登录 ⇒ 匿名导览打上去第 1 步就会 `HTTP 401`。
> **先起服务，再跑导览**（这条命令在 README 里原先只写在"启动 API"一节，试运行一节没写，
> 于是照着试运行一节做必然拿到 `ConnectionRefused`）：

```powershell
# 0. 前置：起 dev 实例（后台 + 日志 data/run/backend-dev.log）
.\.venv\Scripts\python.exe manage.py start --daemon
.\.venv\Scripts\python.exe manage.py status      # 期望：后端 API 8100 本项目运行中

# 七步导览：逐项 PASS/FAIL，含风险声明（试运行开场推荐）
.\.venv\Scripts\python.exe scripts/demo_tour.py
# 含真实行情回测（异步 job，需再等数十秒）
.\.venv\Scripts\python.exe scripts/demo_tour.py --with-backtest
# 第 5 步"调度管理"是管理员专属：不带凭据时它断言"匿名必须被拒"（401/403 即 PASS）；
# 想读真实作业清单就带上管理员会话 Cookie
.\.venv\Scripts\python.exe scripts/demo_tour.py --admin-cookie "moss_session=..."

# 预热试运行数据（幂等）
.\.venv\Scripts\python.exe scripts/seed_demo_data.py

# 集合竞价规则总表 / 单票判定溯源（**商业版**，目录不入库时命令会缺）
.\.venv\Scripts\python.exe -m src.auction_select.rulebook --dump
.\.venv\Scripts\python.exe -m src.auction_select.rulebook --explain 600630 --trade-date 20260918

# 端到端工程冒烟
.\.venv\Scripts\python.exe scripts/_smoke_e2e.py

# 回测引擎独立试运行（--synthetic 用确定性合成数据验证机制）
.\.venv\Scripts\python.exe scripts/backtest_demo.py
```

**完整试运行动线见 [`docs/DEMO_GUIDE.md`](docs/DEMO_GUIDE.md)。**

### 多用户化：管理员与审批（**第一次跑必须先做这一步**）

本系统的注册是**审批制**：新用户注册后状态为 `pending`、**不能登录**，
必须由管理员在「用户管理」里放行。而管理台本身需要管理员身份 ——
所以**第一个管理员不可能从界面产生**，必须由部署者用服务器访问权显式创建：

```powershell
# 创建首个管理员（会打印一次初始密码，请立刻保存）
.\.venv\Scripts\python.exe scripts/bootstrap_admin.py `
    --username admin --email you@example.com --db data/dev/moss_dev.db

# 查看当前有哪些管理员 / 把已有用户提升为管理员
.\.venv\Scripts\python.exe scripts/bootstrap_admin.py --list --db data/dev/moss_dev.db
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

两条刻意的限制（**服务端强制**）：

1. **管理员不能改自己的套餐等级、也不能停用/删除自己** ——
   否则会失去唯一的进入管理台的入口，只能改库才能救回来；
2. **每个管理动作都记流水**（谁、对谁、改了什么、从什么改成什么）——
   匿名管理员在合规上等于没有管理员（四眼原则要求操作可归因到人）。

### 多用户化：普通用户能用的三个页面

登录后顶栏新增两组入口，都是**按用户隔离**的：

| 页面 | 隔离口径 |
|---|---|
| **我的自选池** | ⚠️ **该页面已于 2026-09-23 删除**（用户口径："很鸡肋，不需要了"）——`my_pools` 路由与 `intradayWatchlistMine` 都已移除，"加/删自选"直接写 `configs/intraday.yaml`。**自选目前仍是全站一份**，按账号隔离属待办（`docs/PRD.md` §50.6，`CHG-0224`） |
| **自定义板块** | ✅ 按账号隔离（2026-10-08，`CHG-0223`）：归属键 `user_id`、`UNIQUE(user_id, name)`、越权 404；额度 20×200（试用 5×50）。见 `docs/PRD.md` §50.5 |
| **个股口径** | 同一只票两人各有各的一份；`from_user` 标明是"已自定义"还是"跟随系统默认" |
| **用户管理**（仅管理员） | 服务端每个端点强制 `applied_tier == 'admin'` |

**额度由服务端按套餐强制**（`applied_tier` → 额度表只在
`src/domain/quota/service.py` 定义一处）：

| 套餐 | 池数 | 单池 | 自选总数（去重） |
|---|---|---|---|
| 管理员 | 5 | 100 | 100 |
| VIP | 5 | 100 | 100 |
| 试用 | 5 | 5 | 25 |

板块成员**不占**自选总数（也不进实时取数宇宙）—— 否则"我加了个板块，自选就满了"。

---

## CI / 合规门禁

`.github/workflows/ci.yml` 三段作业，全是常驻契约：

| 作业 | 内容 | 失败处置 |
|---|---|---|
| **Backend** | Python 3.11 + 3.13；`uv sync` → ruff lint → 全量 pytest | 阻断 |
| **Frontend** | Node 20；`npm ci` → 类型检查 → `npm run build` | 阻断 |
| **Compliance** | `test_compliance_gate` + `test_tenancy` + `verify_cleanup` + `test_market_constants` + `test_trading_session` + 敏感信息扫描 + `prd_sync_check --self-test/--ledger` + `test_auction_golden` | **阻断合并** |

合规 job 与普通单测失败的处置**不同**——它挂了**不允许合并**（每条用例对应一条监管/内控要求）。

需求对账门禁（`prd_sync_check.py`）：

```bash
uv run python scripts/prd_sync_check.py --keyword "<关键词>"  # 三态结论
uv run python scripts/prd_sync_check.py --ledger                # 台账完整性（交付前 0 ERROR）
uv run python scripts/prd_sync_check.py --self-test            # 检查器本身可信
```

纪律全文：`.trae/skills/requirement-prd-sync/SKILL.md`；
台账：`docs/REQUIREMENT_CHANGELOG.md`；
证据基座：`docs/PRD_ALIGNMENT_AUDIT_20260928.md`。

---

## 已知不足

不粉饰，主动列出来：

| 不足 | 现状 | 改进路径 |
|---|---|---|
| **单进程 + SQLite** | 当前阶段的刻意选择（零依赖好部署） | 仓储层已抽象，换 PostgreSQL 只改配置 |
| **外部联网源白名单紧** | 博查/百度两源分别限额（总量 1000 / 日 100） | 接其他云端并显式退出默认本地优先 |
| **语义缓存是字符 3-gram 余弦** | 轻量无依赖，长 prompt 下区分度下降 | 换 embedding + 向量库；长 prompt 下关闭语义层只留精确层 |
| **成本核算链路未闭环** | `models.yaml` 写了定价块但网关不读，只统计 token 不折钱 | 网关侧按 token × 单价写 `cost_yuan` 进审计 |
| **大文件** | `src/intraday/service.py` 2526 行、`src/auction_select/sources.py` 1482 行 | 按职责拆分（见 `docs/PROJECT_AUDIT_2026-09-19.md`） |
| **回测无实盘验证** | 三档模型回测有**样本内声明**与滑点敏感性 | 滚动样本外 + 交易成本细化 |
| **公开版不含商业模块** | `src/auction_select/`、`moss_selector/`、`src/intraday/niuline.py` 等目录在 `.gitignore`，克隆后**不会**入库；`src/api/routes/__init__.py` 走 try/except 静默跳过 | 本地持有商业版模块即恢复接口层路由注册 |

---

## 文档索引

### 架构与设计

| 文档 | 内容 |
|---|---|
| [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) | 系统架构 |
| [`docs/ARCHITECTURE_DEEP_DIVE.md`](docs/ARCHITECTURE_DEEP_DIVE.md) | 架构深读 |
| [`docs/ARCHITECTURE_PANORAMA_20260930.md`](docs/ARCHITECTURE_PANORAMA_20260930.md) | 全景图（2026-09-30） |
| [`docs/FRONTEND_DESIGN.md`](docs/FRONTEND_DESIGN.md) | 前端设计 |
| [`docs/OBSERVABILITY.md`](docs/OBSERVABILITY.md) | 指标、审计链与可观测性 |
| [`docs/SCHEDULER_DESIGN.md`](docs/SCHEDULER_DESIGN.md) | 调度器设计 |
| [`docs/RUNTIME_SAFETY_ARCHITECTURE.md`](docs/RUNTIME_SAFETY_ARCHITECTURE.md) | 运行时安全架构 |

### 项目材料与归档

| 文档 | 内容 |
|---|---|
| [`docs/RESUME_PROJECT.md`](docs/RESUME_PROJECT.md) | **项目介绍（A/B 版 + 深度问答）** |
| [`docs/DESIGN_HIGHLIGHTS.md`](docs/DESIGN_HIGHLIGHTS.md) | 设计亮点与难点 |
| [`docs/DEMO_GUIDE.md`](docs/DEMO_GUIDE.md) | 试运行动线与话术 |
| [`docs/INTERVIEW_FINAL.md`](docs/INTERVIEW_FINAL.md) | 深度问答整合档（最权威） |
| [`docs/INTERVIEW_PLAYBOOK.md`](docs/INTERVIEW_PLAYBOOK.md) | 深度问答手册 |
| [`docs/INTERVIEW_NOTES.md`](docs/INTERVIEW_NOTES.md) | 问答备忘 |
| [`docs/INTERVIEW_TECH_PANORAMA_20260930.md`](docs/INTERVIEW_TECH_PANORAMA_20260930.md) | 技术全景 |

### 安全 / 合规 / 多租户

| 文档 | 内容 |
|---|---|
| [`docs/SECURITY_COMPLIANCE.md`](docs/SECURITY_COMPLIANCE.md) | 安全合规 |
| [`docs/MULTI_TENANCY_DESIGN.md`](docs/MULTI_TENANCY_DESIGN.md) | 多租户与合规设计（RBAC × ABAC × 信息隔离墙） |
| [`docs/PLATFORM_MULTI_TENANCY_DESIGN.md`](docs/PLATFORM_MULTI_TENANCY_DESIGN.md) | 平台多租户与权限设计（三级资源归属 + 模块级能力码 + RLS 双层隔离） |
| [`docs/CODE_AUDIT_CHECKLIST.md`](docs/CODE_AUDIT_CHECKLIST.md) | 代码审计清单 |

### 业务模块

| 文档 | 内容 |
|---|---|
| [`docs/AUCTION_SELECT.md`](docs/AUCTION_SELECT.md) | 集合竞价选股（规则总表、口径核验） |
| [`docs/AUCTION_DATA_REDUNDANCY.md`](docs/AUCTION_DATA_REDUNDANCY.md) | 集合竞价数据冗余链路审计 |
| [`docs/INTRADAY_T_DESIGN.md`](docs/INTRADAY_T_DESIGN.md) | 做 T 辅助模块设计 |
| [`docs/INTRADAY_T_OPERATION_GUIDE.md`](docs/INTRADAY_T_OPERATION_GUIDE.md) | 做 T 运营指南 |
| [`docs/QUANT_DATA_WAREHOUSE.md`](docs/QUANT_DATA_WAREHOUSE.md) | 量化数据仓库 |
| [`docs/QUANT_M1_DATA_LAYER.md`](docs/QUANT_M1_DATA_LAYER.md) | 量化数据层 |
| [`docs/QUANT_M2_FACTORS.md`](docs/QUANT_M2_FACTORS.md) | 因子库与 IC/IR |
| [`docs/QUANT_M4_SINGLE_BACKTEST.md`](docs/QUANT_M4_SINGLE_BACKTEST.md) | 单标的回测 |
| [`docs/QUANT_SELECT.md`](docs/QUANT_SELECT.md) | 量化选股 |
| [`docs/QUANT_STRATEGY_CASES.md`](docs/QUANT_STRATEGY_CASES.md) | 量化策略案例 |
| [`docs/SHORTTERM_BACKTEST.md`](docs/SHORTTERM_BACKTEST.md) | 短线回测 |
| [`docs/SELECTOR_DAILY_K.md`](docs/SELECTOR_DAILY_K.md) | 日 K 选股 |
| [`docs/SECTOR_CROWDING.md`](docs/SECTOR_CROWDING.md) | 板块拥挤度 |
| [`docs/SECTOR_ROTATION.md`](docs/SECTOR_ROTATION.md) | 行业轮动日报 |
| [`docs/FUND_FLOW_MONITOR.md`](docs/FUND_FLOW_MONITOR.md) | 资金流监控 |
| [`docs/MAINLINE_MINING.md`](docs/MAINLINE_MINING.md) | 主线挖掘（三层漏斗 / 期货 / Walk-Forward） |
| [`docs/ETF_FLOW_MONITOR.md`](docs/ETF_FLOW_MONITOR.md) | ETF 份额监控 |
| [`docs/ETF_FLOW_DATA_QUALITY.md`](docs/ETF_FLOW_DATA_QUALITY.md) | ETF 数据质量 |
| [`docs/ETF_FLOW_BACKTEST.md`](docs/ETF_FLOW_BACKTEST.md) | ETF 份额信号回测报告 |
| [`docs/ETF_BOARD_MAPPING.md`](docs/ETF_BOARD_MAPPING.md) | ETF 板块映射 |
| [`docs/ETF_REGIME_RULES_BACKTEST.md`](docs/ETF_REGIME_RULES_BACKTEST.md) | ETF 状态规则回测 |
| [`docs/INTEGRATION_PLAN_zsxq_intel.md`](docs/INTEGRATION_PLAN_zsxq_intel.md) | 知识星球情报接入 |
| [`docs/INTEL_CENTER_REDESIGN.md`](docs/INTEL_CENTER_REDESIGN.md) | 情报中心重构 |
| [`docs/INTEL_PERMISSION_DESIGN.md`](docs/INTEL_PERMISSION_DESIGN.md) | 情报权限设计 |
| [`docs/INVESTMENT_CALENDAR_DESIGN.md`](docs/INVESTMENT_CALENDAR_DESIGN.md) | 投研日历设计 |

### 性能 / 数据 / 链路

| 文档 | 内容 |
|---|---|
| [`docs/PERFORMANCE_OPTIMIZATION_2026-09-15.md`](docs/PERFORMANCE_OPTIMIZATION_2026-09-15.md) | 性能优化（2026-09-15） |
| [`docs/END_TO_END_OPTIMIZATION_2026-09-28.md`](docs/END_TO_END_OPTIMIZATION_2026-09-28.md) | 端到端优化（2026-09-28） |
| [`docs/OPTIMIZATION_SUMMARY_2026-09-27.md`](docs/OPTIMIZATION_SUMMARY_2026-09-27.md) | 优化汇总（2026-09-27） |
| [`docs/LATENCY_OPTIMIZATION.md`](docs/LATENCY_OPTIMIZATION.md) | 延迟优化 |
| [`docs/PANEL_LOAD_LATENCY.md`](docs/PANEL_LOAD_LATENCY.md) | 面板加载延迟 |
| [`docs/PANEL_PREFETCH_KEEPALIVE.md`](docs/PANEL_PREFETCH_KEEPALIVE.md) | 面板预取与 KeepAlive |
| [`docs/TOKEN_OPTIMIZATION.md`](docs/TOKEN_OPTIMIZATION.md) | Token 优化 |
| [`docs/LLM_ROUTING.md`](docs/LLM_ROUTING.md) | LLM 路由 |
| [`docs/LLM_MODEL_SELECTION_RESEARCH_20260928.md`](docs/LLM_MODEL_SELECTION_RESEARCH_20260928.md) | LLM 模型选型调研 |
| [`docs/DATA_ACQUISITION_FLOW.md`](docs/DATA_ACQUISITION_FLOW.md) | 数据获取流程 |
| [`docs/DATA_CONTRACT.md`](docs/DATA_CONTRACT.md) | 数据契约 |
| [`docs/DATA_FRESHNESS_ARCHITECTURE.md`](docs/DATA_FRESHNESS_ARCHITECTURE.md) | 数据新鲜度架构 |
| [`docs/DATA_INDEX_REQUIREMENTS_AUDIT.md`](docs/DATA_INDEX_REQUIREMENTS_AUDIT.md) | 数据索引需求审计 |
| [`docs/DATA_PIPELINE_DIAGNOSIS.md`](docs/DATA_PIPELINE_DIAGNOSIS.md) | 数据流水线诊断 |
| [`docs/DATA_SOURCE_INTEGRATION.md`](docs/DATA_SOURCE_INTEGRATION.md) | 数据源集成 |
| [`docs/DATA_SOURCE_ROUTING.md`](docs/DATA_SOURCE_ROUTING.md) | 数据源路由 |
| [`docs/SELF_HEALING_DATA_AGENT.md`](docs/SELF_HEALING_DATA_AGENT.md) | 自学习数据补充 |
| [`docs/CACHE_DESIGN.md`](docs/CACHE_DESIGN.md) | 缓存设计 |
| [`docs/CACHE_TEST_REPORT.md`](docs/CACHE_TEST_REPORT.md) | 缓存测试报告 |
| [`docs/CONCURRENCY_CAPACITY_ASSESSMENT.md`](docs/CONCURRENCY_CAPACITY_ASSESSMENT.md) | 并发容量评估 |
| [`docs/CONCURRENCY_IMPLEMENTATION_REPORT.md`](docs/CONCURRENCY_IMPLEMENTATION_REPORT.md) | 并发实现报告 |

### 运维 / 部署

| 文档 | 内容 |
|---|---|
| [`docs/OPS_GUIDE.md`](docs/OPS_GUIDE.md) | 运维手册 |
| [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) | 部署说明 |
| [`docs/HK_VPS_MIGRATION.md`](docs/HK_VPS_MIGRATION.md) | HK VPS 迁移记录 |
| [`docs/PRD.md`](docs/PRD.md) | 需求总览（含 § 19.33 外部联网源、§ 19.19 缺口语义、§ 13 现行口径归档区） |
| [`docs/REQUIREMENT_CHANGELOG.md`](docs/REQUIREMENT_CHANGELOG.md) | 需求变更台账 |
| [`docs/PRD_ALIGNMENT_AUDIT_20260928.md`](docs/PRD_ALIGNMENT_AUDIT_20260928.md) | PRD 对账审计（2026-09-28） |
| [`docs/MANAGE_CLI.md`](docs/MANAGE_CLI.md) | manage.py 命令行 |
| [`docs/ERROR_CODES.md`](docs/ERROR_CODES.md) | 错误码 |
| [`docs/API_REFERENCE.md`](docs/API_REFERENCE.md) | HTTP 接口参考 |
| [`docs/TROUBLESHOOTING.md`](docs/TROUBLESHOOTING.md) | 常见问题排查 |
| [`docs/DEVELOPMENT_ROADMAP.md`](docs/DEVELOPMENT_ROADMAP.md) | 路线图与 Backlog |
| [`docs/ALERT_RETENTION.md`](docs/ALERT_RETENTION.md) | 告警保留期 |

### 商业 / 上下文

| 文档 | 内容 |
|---|---|
| [`docs/COMMERCIALIZATION_CONTEXT.md`](docs/COMMERCIALIZATION_CONTEXT.md) | 商业化上下文 |
| [`docs/PRICING_CONTEXT.md`](docs/PRICING_CONTEXT.md) | 定价上下文 |
| [`docs/CROWDING_METRIC_SCOPE_FACTCHECK.md`](docs/CROWDING_METRIC_SCOPE_FACTCHECK.md) | 拥挤度指标口径复核 |

### 仓库卫生

| 文档 | 内容 |
|---|---|
| [`FILE_CLEANUP_AUDIT.md`](FILE_CLEANUP_AUDIT.md) | **本轮文件清理审计（gitignored / bak / 旧版并列 / untracked）** |
| [`FILE_INDEX.md`](FILE_INDEX.md) | **仓库文件索引与功能说明** |
| [`AGENTS.md`](AGENTS.md) | 项目硬规则（**唯一事实源**） |
| [`CLAUDE.md`](CLAUDE.md) | 转发层（不复制内容） |

---

## 免责声明

本项目输出仅供技术研究，**不构成投资建议**。回测结果不预示未来收益。