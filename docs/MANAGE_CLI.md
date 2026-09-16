# manage.py 统一管理 CLI 使用手册

> 位置：项目根目录 `manage.py`，仅依赖 Python 标准库，无需安装额外包。
> 所有命令在项目根目录执行：`python manage.py <命令> [参数]`（用 venv 的解释器）。
>
- 本文档配套架构说明：[RUNTIME_SAFETY_ARCHITECTURE.md](./RUNTIME_SAFETY_ARCHITECTURE.md)

---

## 0. 30 秒速查

| 我想做什么 | 命令 |
|------------|------|
| 本地开发调试（看实时日志） | `python manage.py start --reload` |
| 后台挂着跑（前后端一起） | `python manage.py start --daemon --with-frontend` |
| 看所有服务/依赖状态 | `python manage.py status` |
| 重启后端（部署新版本） | `python manage.py start --replace` |
| 跑测试（**不会影响线上数据**） | `python manage.py test -q` |
| 停掉本项目所有服务 | `python manage.py stop` |
| 看后台服务日志 | `python manage.py logs -f` |
| 构建前端生产包 | `python manage.py build` |
| 生产分布式调度（需Redis） | `python manage.py worker --daemon` |

---

## 1. `start` —— 启动后端 API（含前端联调）

```
python manage.py start [--host 127.0.0.1] [--port 8100]
                      [--reload] [--daemon] [--with-frontend]
                      [--replace] [--auto-port]
```

**默认行为**：前台启动 FastAPI 服务 `http://127.0.0.1:8100`，日志直接输出在终端，
`Ctrl+C` 停止。后端进程内自带 CronScheduler（定时采集/告警扫描），**演示模式不需要单独起调度器**。

### 参数与适用场景

| 参数 | 作用 | 适用场景 |
|------|------|----------|
| （无参数） | 前台运行，日志直出 | 日常开发、快速验证 |
| `--reload` | 代码变更自动重启（uvicorn热重载） | 改后端代码时持续调试 |
| `--daemon` | 后台运行，日志写 `data/run/backend.log`，PID 写 `data/run/backend.pid` | 服务常驻、关掉终端不退出 |
| `--with-frontend` | 同时后台拉起前端 dev server（5173） | 联调、演示 |
| `--replace` | 若 8100 被**本项目旧实例**占用，先精确停止旧实例再启动 | 发版重启 |
| `--auto-port` | 端口冲突时自动 +1 寻找空闲端口 | 多实例并行调试 |
| `--host/--port` | 自定义监听地址端口 | 特殊网络环境 |

### 安全行为（重要）

- 端口已被**本项目实例**占用且没加 `--replace`：提示"已在运行"，**不会重复拉起**；
- 端口被**其他程序**占用（如 python http.server、别的业务）：**拒绝启动并打印对方 PID/命令行**，
  绝不会杀掉或影响对方进程（实测验证：外部服务在拒绝启动后仍正常返回 200）。

---

## 2. `stop` —— 停止本项目服务

```
python manage.py stop
```

一次停止：后端（8100）、前端 dev（5173）、Celery worker（如存在）。

- 只按 **PID 精确树杀**（`taskkill /T /F /PID`，连带 uvicorn reload/npm 派生子进程）；
- PID 文件丢失时按端口兜底，但必须先通过 `/health` 服务签名或命令行
  证实是"本项目实例"才会停止，**不会误杀同机其他 Node/Python 服务**；
- 严禁使用 `taskkill /F /IM python.exe` / `node.exe` 这类进程名通杀。

---

## 3. `status` —— 服务与外部依赖体检

```
python manage.py status
```

输出一张表：后端 API（8100，含 `/health` 的 status/ollama/deepseek 状态）、
前端 dev（5173）、Celery 调度、Ollama（11434）、XtMiniQmt（58610）。

**适用场景**：启动后确认服务是否真正就绪；排查"页面报错是不是依赖没起"。
注意 XtMiniQmt 未运行时系统会自动降级到本地 CSV/AkShare，不影响主链路。

---

## 4. `test` —— 隔离环境跑测试（推荐入口）

```
python manage.py test [任意 pytest 参数...]
```

示例：

```bash
python manage.py test -q                          # 全量
python manage.py test tests/unit -k circuit      # 按关键字
python manage.py test tests/integration/test_api.py -v
```

**为什么必须用这个入口而不是直接 `pytest`？**
它会在启动前把 SQLite、LLM 审计、调度记录、缓存全部重定向到一次性临时目录
（`%TEMP%\moss_finagent_test_env_*`），关闭语义/Redis缓存，
**测试可以在线上服务运行时执行，对商业用户零影响**。详见架构文档第 2 节。

退出码与 pytest 一致（0=全过），CI 可直接使用。

---

## 5. `frontend` —— 仅启动前端 dev server

```
python manage.py frontend [--daemon]
```

启动 Vite 开发服务器 `http://127.0.0.1:5173`，`/api` 自动代理到 8100 后端。
适用：后端已由他人/另一终端启动，你只调前端。

---

## 6. `build` —— 构建前端生产包

```
python manage.py build
```

执行 `tsc -b && vite build`，产物输出到 `web/dist/`。
该目录存在时，后端会自动用 StaticFiles 同服务托管前端，
因此**生产部署只需启动一个 8100 端口**，不需要单独跑前端进程。

---

## 7. `worker` —— Celery 生产调度（可选，需 Redis）

```
python manage.py worker [--daemon] [--replace] [--pool solo] [--loglevel info]
```

启动 Celery worker + beat，执行 [JOB_REGISTRY](../src/scheduler/registry.py) 中声明的定时任务。

- **本地/演示环境不需要启动**：后端 lifespan 已内置进程内 CronScheduler；
- 仅在 `DATA_BACKEND=postgres`、`REDIS_CACHE_ENABLED=true` 的生产/分布式环境使用；
- Windows 自动使用 `--pool=solo`；
- 停止：`python manage.py stop`（按 `data/run/worker.pid` 精确树杀）。

---

## 8. `logs` —— 查看后台日志

```
python manage.py logs [backend|frontend|worker] [--lines 200] [-f]
```

| 参数 | 作用 |
|------|------|
| 服务名 | 默认 `backend`，可选 `frontend`/`worker` |
| `--lines N` | 打印最后 N 行（默认 200） |
| `-f, --follow` | 持续跟随新日志（Ctrl+C 退出，等同 tail -f） |

只对 `--daemon` 方式启动的服务有效（前台运行时日志直接在终端）。

---

## 9. 运行时文件约定

所有运行态文件都在 `data/run/`（已被 `.gitignore` 忽略）：

```
data/run/backend.pid / backend.log
data/run/frontend.pid / frontend.log
data/run/worker.pid / worker.log
```

PID 文件只在进程存活时有效，CLI 每次使用前都会校验进程是否还在。

---

## 10. 典型工作流

**日常开发**
```bash
python manage.py start --reload          # 终端A：后端热重载
python manage.py frontend                # 终端B：前端
python manage.py test -k xxx             # 随时跑隔离测试，不影响任何服务
```

**演示/面试 Demo**
```bash
python manage.py build                   # 先构建前端
python manage.py start --daemon          # 单端口8100同时提供API+页面
python manage.py status                  # 确认健康
# 浏览器访问 http://127.0.0.1:8100
```

**版本更新/重启**
```bash
python manage.py stop
git pull
python manage.py build
python manage.py start --daemon --replace
python manage.py logs -f                 # 观察启动日志
```

**紧急情况：端口被陌生程序占用**
```bash
python manage.py status                 # 先看是谁
python manage.py start --auto-port      # 自动避让，不要手动杀进程
```
