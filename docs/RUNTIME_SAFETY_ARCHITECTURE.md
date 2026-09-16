# 运行时安全架构：测试隔离与进程管理

> 本文档说明 Moss-FinAgent-Research 在"开发/测试不影响线上商业用户"这一目标下的两道防线：
> ① 测试数据隔离；② 进程身份识别与精确生命周期管理。
>
> 配套使用手册：[MANAGE_CLI.md](./MANAGE_CLI.md)
> 代码入口：根目录 [manage.py](../manage.py)

---

## 1. 设计目标与威胁模型

单机同时存在两类活动，且共用同一台开发机：

| 活动 | 特征 | 风险 |
|------|------|------|
| 线上/演示服务 | uvicorn 监听 8100，SQLite 落 `data/`，定时任务在跑 | 被测试污染数据、被误杀进程 |
| 本地测试/调试 | pytest 高频运行、可能起临时服务、用假 LLM | 写脏生产库、缓存串台、端口冲突时误杀 |

核心原则（源自 moss-finance-assistant 的事故教训）：

1. **全局 `taskkill /F /IM python.exe` / `node.exe` 是禁止操作**——同机可能有无关业务；
2. 只处理 `LISTENING` 占用（`TIME_WAIT` 不影响新 bind，避免误判）；
3. 停止任何进程前必须完成**身份识别**；
4. 测试的"安全"不能靠自觉，必须由代码强制隔离。

---

## 2. 第一道防线：测试数据隔离（纵深两层）

### 2.1 第一层：conftest 临时仓库

[tests/conftest.py](../tests/conftest.py) 用 `tempfile.mkdtemp()` 创建一次性目录，
`repo` fixture 指向 `tmp_dir/test.db`，**生产库 `data/moss_finagent.db` 从不被测试触碰**。
集成测试使用 httpx ASGI 内存传输（`ASGITransport`），不绑定真实端口，
因此可以与 8100 上的真实服务同时运行。LLM 全部 Fake/monkeypatch，测试不产生 DeepSeek 计费。

### 2.2 第二层：`manage.py test` 环境变量级隔离

conftest 只隔离了仓库；为覆盖审计日志、缓存、调度记录等旁路写入，
`manage.py test` 在 pytest 启动前注入环境变量并清空 settings 单例：

```
MOSS_ENV=test
SQLITE_PATH=<tmp>/test.db
REDIS_CACHE_ENABLED=false
LLM_CACHE_ENABLED=false        # 禁止跨运行复用语义缓存，保证可重复
LLM_AUDIT_DIR=<tmp>/audit      # 审计JSONL
SCHEDULER_DIR=<tmp>/scheduler  # 定时任务运行记录
```

pydantic-settings 按字段名读取这些变量，
而 `EventSqliteRepository(settings.sqlite_path)` 等组件统一从 settings 取路径，
因此**一处注入、全链路重定向**。conftest 显式临时路径仍然优先，两层不冲突。

### 2.3 隔离链路图

```
manage.py test
   │  注入环境变量 + get_settings.cache_clear()
   ▼
pytest ──► conftest repo fixture ──► %TEMP%/moss_finagent_test_env_xxx/test.db
   │
   ├─ LLM 审计  ──► .../audit/（临时目录）
   ├─ 调度记录  ──► .../scheduler/（临时目录）
   ├─ 语义缓存  ──► 关闭（LLM_CACHE_ENABLED=false）
   ├─ Redis     ──► 关闭（REDIS_CACHE_ENABLED=false）
   ├─ HTTP      ──► ASGI 内存传输，不占端口
   └─ LLM 调用  ──► Fake/monkeypatch，零真实计费
                          │
            生产 data/moss_finagent.db + 8100服务  ◄── 完全不相交
```

### 2.4 验证证据

全量 513 项测试通过 `manage.py test` 在 8100 商业实例运行期间执行，
服务 `/health` 全程 healthy，生产 `data/` 目录无测试写入。

### 2.5 后续新增测试的约束

- 新建 fixture 涉及落盘时，路径必须来自 `tmp_path` / conftest 临时目录；
- 禁止在测试中硬编码 `data/...` 相对路径；
- 新增任何"旁路持久化"组件时，路径必须可经 settings/环境变量重定向，
  并同步登记到 [manage.py](../manage.py) 的 `build_test_env()`。

---

## 3. 第二道防线：进程身份识别与精确生命周期

### 3.1 服务签名

`/api/v1/health` 返回体带唯一标识：

```json
{ "service": "moss-finagent-research", "status": "...", ... }
```

CLI 通过两个独立证据判定端口上的进程是不是"自己人"：

1. HTTP 签名：`GET /api/v1/health` 的 `service` 字段；
2. 进程命令行：Windows 下经 `Get-CimInstance Win32_Process` 取命令行，
   匹配 `src.api.main:app`。

两者满足其一即认定为本项目实例；都不满足则视为**外部程序，受保护，不碰**。

### 3.2 端口诊断状态机（`diagnose_port`）

```
TCP connect 探测
   │
   ├─ 连不上 ──► 空闲，允许启动
   │
   └─ 能连上 ──► netstat -ano 查 LISTENING PID（只认 LISTENING）
                   │
                   ├─ /health签名==本项目 或 命令行含 src.api.main:app
                   │        ├─ start（无--replace）► 提示"已在运行"，退出0，不重复启动
                   │        └─ start --replace     ► PID 精确树杀后重启
                   │
                   └─ 外部程序
                            └─ 拒绝启动（exit 2），打印对方PID+命令行，
                               建议 --auto-port；绝不杀进程
```

实测：在 48999 端口启动一个无关 `python -m http.server`，
`start --port 48999` 返回拒绝（exit 2），事后该 HTTP 服务仍正常响应 200。

### 3.3 精确停止（`stop`）

- 依据 `data/run/<service>.pid`，用 `taskkill /T /F /PID <pid>` 做**进程树终止**
  （`/T` 连带 uvicorn `--reload` 父子进程、npm→node 派生链、celery worker→beat）；
- PID 文件丢失时才按端口兜底，且兜底同样先做身份识别；
- 外部程序占用端口时只打印告警并跳过；
- 覆盖三类本项目进程：backend（8100）、frontend（5173）、worker（无端口，纯 PID 文件）。

### 3.4 后台运行与可观测性

`--daemon` 启动的进程为 DETACHED 独立进程组，生命周期与终端解耦：

```
data/run/backend.{pid,log}
data/run/frontend.{pid,log}
data/run/worker.{pid,log}
```

`logs [-f]` 读尾部/跟随；`status` 聚合端口探测、PID 存活与 `/health` 详情；
`data/run/` 已整体被 `.gitignore` 忽略。

### 3.5 启动就绪判定

daemon 模式不是"启动即宣称成功"：轮询端口最多 ~10s，
进程提前退出时立即报错并指向日志文件，避免"假启动"。

---

## 4. CLI 启停场景覆盖审计

### 4.1 已覆盖

| 场景 | 命令 | 身份/隔离机制 |
|------|------|---------------|
| 后端前台开发 | `start` / `start --reload` | 端口诊断 |
| 后端后台常驻 | `start --daemon` | PID+日志+就绪轮询 |
| 发版重启 | `start --replace` | 签名识别后精确树杀 |
| 前后端一起起 | `start --with-frontend` | 各自独立 PID |
| 端口被外部占用 | `start` 拒绝 / `--auto-port` 避让 | 外部进程受保护 |
| 仅前端开发 | `frontend` | PID 文件 |
| 前端生产构建 | `build`（产物由后端同端口托管） | — |
| 隔离测试 | `test ...` | 环境变量+临时目录 |
| 生产分布式调度 | `worker [--daemon]`（Celery+beat，需Redis） | PID 精确管理 |
| 查看日志 | `logs [-f]` | — |
| 一键停止 | `stop` | backend+frontend+worker |
| 健康体检 | `status` | 端口+/health+PID |

### 4.2 明确不纳入 CLI 管理（边界）

| 对象 | 原因 | CLI 态度 |
|------|------|----------|
| Ollama（11434） | 桌面安装的模型服务，可能被其他项目共用 | `status` 只探测不启停 |
| XtMiniQmt（58610） | 迅投券商客户端，需人工登录 | `status` 探测；不可用自动降级 CSV/AkShare |
| Redis / PostgreSQL | 外部基础设施，生命周期归运维 | 只探测/连接，不启停 |
| 一次性脚本（download_qmt_data.py、seed_demo_data.py、backtest_demo.py） | 非长驻服务，按需手动执行 | 不包装为服务命令 |
| 单 Agent 手动调用 | 应走 HTTP API，而非进程管理 | 不提供 |

### 4.3 未来扩展点（当前刻意不做）

- 多实例/多租户并行：需要 `--instance-name` 隔离 PID/日志命名空间；
- Linux systemd / Windows 服务注册：部署形态确定后再加 `install-service`；
- worker 的 beat 与 worker 拆分进程：当前演示 `-B` 合一即可。

---

## 5. 测试策略

[tests/unit/test_manage_cli.py](../tests/unit/test_manage_cli.py) 只测**纯函数与临时 socket**，
不真正启停服务，保证测试本身快速、无副作用：

- 命令行身份识别（本项目/外部/空值）；
- 测试环境变量重定向（路径全部落在临时目录，不含生产库名）；
- PID 文件（缺失/非法/死进程/存活进程）；
- 端口探测（临时起本地 socket 验证 LISTENING 检测）；
- argparse 全子命令与 pytest 参数透传。

红线：**CLI 测试不得调用 `start/stop` 真实命令**，避免在开发机/CI 上杀进程。

