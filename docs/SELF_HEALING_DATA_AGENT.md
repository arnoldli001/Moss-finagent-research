# 自学习数据补充能力——技术设计文档

> 版本 2026-09-15 | 实现参见 `src/domain/agents/data_gap_resolver.py`

---

## 一、架构设计

### 1.1 问题定义

投研分析系统中，用户输入的查询可能涉及**系统尚未接入的指标**（如"半导体销售额同比"、"船舶订单量"）。传统做法是开发者手动编写连接器、注册路由、配置调度——周期长、响应慢、不可扩展。

本模块实现**自学习数据补充**：当指标采集返回空时，系统自动发现数据源、生成采集代码、验证安全性、注册到运行时、注册定时调度，并将结果**沉淀为持久资产**，后续请求直接命中本地 DB。

### 1.2 核心流程

```
用户投研分析请求
  ↓
A01 并发采集 N 个指标（asyncio.gather）
  ↓ 某指标 fetch 返回空
  ↓ _storage_fallback 查 DB 也没数据
  ↓
触发 _try_self_heal(indicator, error_context)
  ↓
DataGapResolverAgent.resolve()
  ├── [1] LLM 生成连接器代码（reasoning 层，含模板+白名单约束）
  ├── [2] validate_connector_code() 静态 AST 验证
  ├── [3] sandbox_test() 子进程冒烟（15s 超时 kill）
  ├── [4] 写入 data/dynamic_connectors/gap_xxxx.py
  ├── [5] DynamicConnectorLoader.reload() 热加载
  ├── [6] 重试 fetch 验证新连接器确实能拿到数据
  ├── [7] register_dynamic_job() 注册定时采集
  └── 返回数据（或失败时返回空，不阻断主链路）
```

### 1.3 模块依赖关系

```
┌─────────────────────────────────────────────────────┐
│                  collect_node (supervisor.py)         │
│  A01 并发采集 → _storage_fallback → _try_self_heal   │
└──────────────────────┬──────────────────────────────┘
                       │
         ┌─────────────▼──────────────┐
         │   DataGapResolverAgent      │
         │   (data_gap_resolver.py)    │
         │                             │
         │  LLM Gateway ──→ 生成代码   │
         │  CodeValidator ──→ 验证    │
         │  sandbox_test ──→ 沙箱执行  │
         │  DynamicConnectorLoader     │
         │    ──→ 热加载到 Router      │
         │  register_dynamic_job       │
         │    ──→ 注册定时调度          │
         └─────────────────────────────┘
```

### 1.4 三级降级链（与已有架构无缝衔接）

```
[1] 进程内 TTL 缓存（毫秒级）
[2] 本地持久化 DB（SQLite，慢变量查 DB + 过期判定）
[3] 真实 connector 链（QMT→CSV→AkShare 故障转移）
    ↓ 全部返回空
[4] _storage_fallback 查 DB 历史快照
    ↓ 也没有
[5] _try_self_heal 自修复 ← 本模块新增
    ↓ 自修复也失败
[6] 返回空，指标缺口上报 A17 决策 Agent
```

### 1.5 关键设计决策

#### D1: 为什么用 LLM 生成代码而不是预定义模板？

**选项对比**：

| 方案 | 灵活性 | 安全性 | 维护成本 |
|------|--------|--------|----------|
| 预定义模板填充 | 低（只支持已知 API 模式） | 高（模板已审计） | 低 |
| LLM 生成完整代码 | 高（可适配任意 API） | 中（需验证） | 中 |
| LLM 生成 + 人工审核 | 高 | 高 | 高（不自动化） |

**选择 LLM 生成 + 两层自动验证**：投研系统的指标种类不可预测（用户可能问任何行业的任何数据），预定义模板无法覆盖。LLM 可以根据指标名推断 AkShare/公开 API 函数名，生成完整可执行的连接器代码。安全风险通过 AST 白名单 + 子进程沙箱兜底。

#### D2: 为什么最多重试 2 次而不是更多？

第一版失败时，LLM 收到错误信息后修正重生成。第 2 次仍失败说明：
- 指标名太模糊（LLM 不知道该调什么 API）
- AkShare 没有对应函数
- 生成的代码有结构性问题

此时继续重试是浪费 LLM token 和用户时间。2 次后放弃，指标缺口照样上报给 A17——自修复是**增强不是必需**。

#### D3: 为什么自修复在 collect_node 内同步执行？

collect_node 是 LangGraph 的数据采集节点，A01 → A02 → A03 → A04 管线在这里完成。如果自修复异步执行（如 fire-and-forget），当前任务拿不到修复后的数据，用户体验无改善。

同步执行意味着用户可能等 30-60s（LLM 生成 15s + 沙箱 15s + fetch 5s），但修复成功后**该指标在本次任务中就可用**，且后续永久命中 DB。

---

## 二、安全护栏

### 2.1 三层安全验证

```
Layer 1: LLM Prompt 约束
  ├── 白名单导入：akshare/requests/aiohttp/httpx/pandas/numpy/...
  ├── 黑名单调用：os/subprocess/exec/eval/pickle/__import__
  └── 代码模板确保结构合法（BaseConnector 子类 + supports + fetch）

Layer 2: AST 静态验证（validate_connector_code）
  ├── 语法检查：ast.parse() 确保无语法错误
  ├── 导入白名单：遍历 Import/ImportFrom 节点
  ├── 危险调用检测：遍历 Call 节点，匹配 FORBIDDEN_NAMES
  ├── 正则补充：捕获 AST 难以识别的模式
  └── 结构检查：必须含 BaseConnector 子类 + supports + fetch 方法

Layer 3: 子进程沙箱（sandbox_test）
  ├── 独立子进程执行（主进程不受影响）
  ├── 15 秒超时 kill（防止死循环/网络阻塞）
  ├── 导入 + 实例化 + supports/fetch 冒烟
  └── 输出 JSON 结果（ok/class/indicators）
```

### 2.2 禁止的导入和调用

| 类别 | 禁止项 | 原因 |
|------|--------|------|
| 代码执行 | exec, eval, compile | 防止嵌套代码注入 |
| 系统调用 | os.system, os.popen, subprocess | 防止命令注入 |
| 模块加载 | __import__, importlib | 防止动态加载任意模块 |
| 序列化 | pickle, marshal | 防止反序列化攻击 |
| 文件写入 | open(..., "w"/"a") | 连接器只读，不允许写文件 |
| 进程控制 | sys.exit, os.fork, os.spawn | 防止进程操控 |
| 内省 | inspect, ctypes, gc | 防止运行时篡改 |

### 2.3 允许的导入

```python
ALLOWED_IMPORTS = {
    # 标准库（安全子集）
    "asyncio", "logging", "re", "json", "datetime", "time",
    "typing", "collections", "dataclasses", "functools",
    "itertools", "math", "statistics", "hashlib", "base64",
    "uuid", "pathlib", "io",
    # HTTP 请求
    "requests", "aiohttp", "httpx",
    # 数据处理
    "pandas", "numpy",
    # 金融数据源
    "akshare", "tushare", "baostock",
    # 解析
    "openpyxl", "lxml", "bs4", "chardet",
    # 项目内部
    "src.core.exceptions", "src.core.schemas",
    "src.infrastructure.connectors.base",
}
```

---

## 三、持久化机制

### 3.1 三层持久化

```
┌────────────────────────────────────────────────────┐
│            自修复成功后持久化的资产                   │
├────────────┬───────────────┬───────────────────────┤
│  层级       │  存储位置      │  恢复方式             │
├────────────┼───────────────┼───────────────────────┤
│  连接器代码  │  data/dynamic  │  DynamicConnector    │
│            │  _connectors/  │  Loader.load_all()   │
│            │  gap_xxxx.py   │  进程启动时自动加载     │
├────────────┼───────────────┼───────────────────────┤
│  调度配置    │  data/dynamic  │  load_dynamic_jobs() │
│            │  _connectors/  │  从 _schedule.json    │
│            │  _schedule.json│  恢复到 JOB_REGISTRY  │
├────────────┼───────────────┼───────────────────────┤
│  采集数据    │  SQLite       │  Router 三级短路      │
│            │  (DataPoint)   │  DB 命中 → 0.0s 返回  │
└────────────┴───────────────┴───────────────────────┘
```

### 3.2 生命周期

```
第一次请求：
  A01 fetch → 空 → 自修复 → 生成 gap_xxxx.py → reload →
  fetch 成功 → 数据入 SQLite → 注册 _schedule.json

第二次请求（同进程）：
  Router TTL 命中 → 0.0s 返回

第 N 次请求（进程重启后）：
  Router DB 命中 → 0.0s 返回（连接器从文件加载）

定时调度（自修复注册的 cron）：
  每日 16:10 → _collect_through_pipeline → A01→A02→A03→A04
  → 新数据入 SQLite → 下次 DB 命中
```

### 3.3 调度频率自动推断

```python
def get_dynamic_schedule_config(indicator, result):
    ind = indicator.lower()
    if ind.startswith(("cpi", "ppi", "m2", "社融")):
        cron = "0 9 1 * *"       # 月度：每月1日09:00
    elif ind.startswith(("pe(", "pb:", "stock_close")):
        cron = "10 16 * * 1-5"   # 日频：工作日16:10
    else:
        cron = "10 16 * * 1-5"   # 默认日频
```

---

## 四、与已有架构的集成点

| 集成点 | 已有模块 | 本模块新增 |
|--------|----------|-----------|
| 路由器 | ConnectorRouter | `repo=` 参数注入 DB 查询 |
| 动态加载 | DynamicConnectorLoader | `reload()` 热加载新连接器 |
| 代码验证 | CodeValidator | 已有 `validate_connector_code()` + `sandbox_test()` |
| 调度器 | JOB_REGISTRY | 新增 `dynamic_collection` JobKind |
| 调度执行 | `_collect_through_pipeline()` | 新增 `dynamic_collection` 处理分支 |
| Supervisor | `collect_node` | 新增 `_try_self_heal()` 钩子 |
| 运行时 | `build_runtime()` | 启动时调 `load_dynamic_jobs()` |
