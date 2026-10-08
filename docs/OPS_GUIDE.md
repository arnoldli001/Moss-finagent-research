# 运维操作手册（OPS）

> 版本：v1 ｜ 日期：2026-09-25
> 适用：`D:\code\Moss-finagent-research`
> 本文档只写**命令与判据**，不重复设计原理。每条都对应一次真实踩坑。

---

## 0. 30 秒速查（最常用的 8 条）

| 我想做什么 | 命令 |
|---|---|
| 看服务活着没 | `python manage.py status` |
| 恢复对外服务（8110） | `powershell -File scripts\pilot_autostart.ps1` |
| 看后台日志 | `python manage.py logs -f` |
| 看有哪些账号 | `python scripts\reset_admin_password.py --list --db data/pilot/moss_pilot.db` |
| 重置某个账号密码 | `python scripts\reset_admin_password.py --username <name> --db data/pilot/moss_pilot.db --expect-db pilot --clip` |
| 跑测试（不影响线上） | `python manage.py test -q` |
| 体检 SQLite | `python manage.py doctor` |
| 看管理员列表（跨库） | `python scripts\_list_admins_ro.py` |

> **`python` = `.\.venv\Scripts\python.exe`**（venv 里的，不是系统的）
> **⚠️ 永远不要用 `manage.py stop`** —— 见 §3.1

---

## 1. ⚠️ 第一课：你有三个库，别读错

这是本项目**最容易踩、最难自查**的坑：三个库都"能打开"，但只有一个有你的数据。

| 库 | 大小 | 用户数 | 用途 |
|---|---:|---:|---|
| **`data/pilot/moss_pilot.db`** | 1.0 GB | **13** | ✅ **对外试点 —— 公网 `moss.wujiaitool.cn` 用的就是它** |
| `data/dev/moss_dev.db` | 6.8 GB | 5 | 本地 dev 实例（`manage.py start` 默认） |
| `data/moss_finagent.db` | 6.3 GB | **0** | 遗留空库 ← **很多脚本的默认目标** |

### 判据：看后端日志的"认证服务已装配"那一行

```powershell
Select-String -Path data\run\backend.log -Pattern '认证服务已装配' | Select-Object -Last 1
# 输出形如：认证服务已装配：env=pilot db=...\data\pilot/moss_pilot.db notifier=SmtpEmailNotifier
```

**`env=pilot` + 路径含 `pilot` 才是对外服务的那个。**

### 为什么默认会读错

`--env pilot` 是 **`manage.py` 自己的参数**，只在那一个进程里设环境变量。
你在新开的 PowerShell 里跑别的脚本，**没有这个环境**，`get_settings()` 就回落到
默认的 `data/moss_finagent.db`（0 用户）。

**结论：凡操作 pilot 数据的脚本，一律显式带 `--db data/pilot/moss_pilot.db`。**

### 三个库的表差别（一眼识别）

```powershell
# 想知道某个库有没有账号
.\.venv\Scripts\python.exe scripts\reset_admin_password.py --list --db <库路径>
# 0 个账号 + 脚本提示"读错库" = 你选错了
```

---

## 2. 服务启停

### 2.1 对外服务（pilot @ 8110）—— 用包装脚本

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File `
    D:\code\Moss-finagent-research\scripts\pilot_autostart.ps1
```

**为什么不能自己拼命令**：任务以 Local System 身份开机自启，
而 `DEEPSEEK_API_KEY` / `ALERT_SMTP_*` 等不在系统级环境变量里，
`config.py` 又**不会**自动加载 `.env`。
包装脚本先注入 `.env` 再调 `manage.py`，跳过它 = 缺配置启动。

**它实际执行的是**：

```
manage.py start --env pilot --port 8110 --daemon
```

### 2.2 本地 dev（8100）

```powershell
.\.venv\Scripts\python.exe manage.py start            # 前台
.\.venv\Scripts\python.exe manage.py start --daemon   # 后台
.\.venv\Scripts\python.exe manage.py start --reload   # 热重载（开发用）
```

### 2.3 ⚠️ `manage.py stop` 是一条"危险命令"

源码注释原文（`manage.py` `cmd_stop`）：

> 后端：按命令行枚举**所有**实例（含端口外的孤儿）……

**它会同时杀掉 8100 和 8110。** 你启动日志里也警告过：

> ⚠️ **不要用 --replace**：它按命令行枚举本项目**全部**后端正进程，
> 会把正在跑的 dev/主实例一起停掉

| 操作 | 影响范围 |
|---|---|
| `manage.py stop` | **全部**本项目后端（含对外服务） |
| `Stop-Process -Id <PID>` / `taskkill /PID <PID> /T /F` | 只那一个（**但必须先确认那个 PID 是什么**） |

> ### ★★ 2026-09-28 实测事故：`--port 8100` **不能**让 `stop` 只杀 8100
>
> ```powershell
> python manage.py stop --port 8100     # ← 这一行杀掉了 pilot(8110)
> ```
>
> 输出：
>
> ```
> ✅ 已停止 backend (PID=12304)
> ❌ 已停止 backend (PID=20836)     ← 我起的 dev(8100)
> ✅ 已停止 backend (PID=22292)     ← ★ pilot(8110) 的 uvicorn，被一起杀了
> ❌ 已停止 backend (PID=23136)     ← ★ pilot 的父进程
> ```
>
> **为什么会这样**：`cmd_stop` 的枚举判据是"**命令行**里含 `src.api.main:app`
> 且属于本项目"，`--port` 只是它自己收下的参数，**根本不参与筛选**。
> 所以"我传了端口 = 只停那个端口"是一个**假的直觉** —— 它和 `--replace`
> 是同一个陷阱，只是看起来更无辜。
>
> **代价**：客户入口中断约 25 秒。`frpc` 在这一段每 ~2 秒记一条
> `connect to local service [127.0.0.1:8110] error: ... actively refused it`
> （`data/run/frpc.log`），这就是事后确认"哪一段断了"的证据。
> 数据库**没有**损坏（`stop` 是优雅停止，走 lifespan）。
>
> **恢复**（实测有效，约 30 秒）：
>
> ```powershell
> python manage.py start --env pilot --port 8110 --daemon
> # 然后必须验证两件事，不能只看端口在听：
> Invoke-WebRequest http://127.0.0.1:8110/api/v1/health/live -UseBasicParsing   # 200
> Invoke-WebRequest https://hk.wujiaitool.cn/api/v1/health/live -UseBasicParsing # 200
> ```
>
> **纪律**：要单独停一个实例，只有两条路 —— ① 按 PID 杀（先确认那个 PID 是什么）；
> ② 停 pilot 用 `scripts/pilot_autostart.ps1` 对应的流程（§2.4）。
> **任何时候都不要用 `manage.py stop`**，哪怕带了 `--port`。

### 2.3b ★★ 改 `configs/models.yaml` **必须重启**，而且只重启那一个实例才有用

**实测（2026-09-28 23:19 改配置 → 00:13 才发现）**：
`LLMGateway` 在**构造时**把 `configs/models.yaml` 读进内存
（`_load_model_config`），之后**再也不重读**。于是：

| 实例 | 启动时间 | 配置改动 | 实际在用的模型 |
|---|---|---|---|
| dev 8100 | 23:19（改配置之后） | — | ✅ `qwen3.5:4b` |
| **pilot 8110** | **22:06（改配置之前）** | 23:19 换地板 | ❌ **仍是 `qwen3:8b-q4_K_M`** |

审计日志是唯一能看出这件事的地方（`agent_id=intel_extract` 那几条的 `model=` 字段）：
配置解析出来是 4B，**进程里跑的却是 8B** —— 两边都不报错。

**纪律**：改了 `configs/models.yaml` / `configs/*.yaml` 里任何**影响路由**的键之后，
**必须逐个重启实例**（dev 与 pilot 是两份进程，重启一个不影响另一个）。
判据不是"配置里写着 4B"，而是**审计里那一行 `model=`**：

```powershell
# 按 PID 重启 pilot（见 §2.4，绝不用 manage.py stop）
# 重启后核对：配置解析 vs 审计实证
uv run python -c "import sys;sys.path.insert(0,'.');from src.infrastructure.llm.gateway import _load_model_config; s,c,_=_load_model_config('configs/models.yaml'); print([m for m in c['medium'] if s[m].provider=='ollama'])"
# 然后等下一次 intel_tone_extract / 告警扫描，看 data/pilot/audit/llm_audit.jsonl 的 model= 字段
```

> ⚠️ **附带修好的一件事**：本次重启前 `data/run/backend.pid` 指向一个**已不存在**的
> 进程（12108），而真实在跑的是 22:06 启动的另一个 PID —— 值守因此每次 tick 都记
> 「端口被其他程序占用（值守拒绝启动）」并用 `exit 2` 退出，**等于长期没有值守**。
> 重启（走 `manage.py start --daemon`）后 pid 文件重新指向存活的启动器 PID，
> 值守恢复认领。**所以「pilot 20~30 秒消失」这类现象，先查 pid 文件指向谁** ——
> 它比"进程真的被杀了"更常见。

### 2.4 只重启 pilot 单实例的正确做法

```powershell
# ① 找到 pilot 的进程（8110 的监听者及其父进程）
Get-NetTCPConnection -State Listen | Where-Object LocalPort -eq 8110 |
    Select-Object LocalPort, OwningProcess
# 再把父进程也找出来
Get-CimInstance Win32_Process -Filter "ProcessId=<PID>" |
    Select-Object ProcessId, ParentProcessId, CommandLine

# ② 强杀整棵树（任务栏里那两个 PID，父+子）
taskkill /PID <父PID> /T /F
taskkill /PID <子PID> /T /F

# ③ 用包装脚本重启
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\pilot_autostart.ps1

# ④ 验证
Start-Sleep 8
Get-NetTCPConnection -State Listen | Where-Object LocalPort -eq 8110
```

> **`taskkill` 不带 `/F` 杀不掉 uvicorn**（它要 CTRL_BREAK 才会优雅退出）。
> 但**必须连子进程一起杀** —— 否则子进程变孤儿占着 SQLite 的 `-wal`/`-shm`，
> 就是"重启后前端几十秒没数据"的根因。

### 2.5 服务健康判据

```powershell
# 本机
.\.venv\Scripts\python.exe manage.py status

# 公网（401 = 后端活着且需鉴权；502 = 回源失败）
try { Invoke-WebRequest https://moss.wujiaitool.cn/api/v1/health -TimeoutSec 20 } catch {
    $_.Exception.Response.StatusCode.value__ }
```

| 现象 | 含义 |
|---|---|
| **HTTP 401** | ✅ 正常（`/health` 需鉴权） |
| **HTTP 502** | ❌ 后端没起 / 端口不对 → 查 §2.1 |
| 连不上 | ❌ cloudflared 挂了 → `Get-Process cloudflared` |

### 2.6 ⚠️ 孤儿进程：为什么会发生、怎么根治

**实测现象**：一个 pytest 进程成了孤儿 —— 父进程已退出、**CPU = 0 秒、
HandleCount = 0、内存 1 MB**，从启动起就卡死，**一天多没动过**，
却一直占着资源与数据库文件句柄。（另一次同类：全量测试跑到 82% 停住，
13.5 分钟 CPU 零增长。）

**根因两条，缺一不可**：

1. **Windows 没有 `PR_SET_PDEATHSIG`** —— 内核**不会**在父进程死亡时
   自动回收子进程。这是 Linux 上的默认配套机制，Windows 上不存在。
2. **pytest 自己不检测父进程** —— 父一死它就成了无主进程继续挂着，
   而它卡住的位置连超时都没有。

于是形成稳定复现的配方：**pytest 因某个用例挂住 → 宿主（AI 工具调用 /
交互式 shell）超时或被强杀 → pytest 变孤儿，永久留下。**

**根治：经 Job Object 运行**（已落地，不依赖任何人记得清理）

```powershell
# ✅ 推荐：隔离环境 + Job Object（父死子亡，内核保证）
.\.venv\Scripts\python.exe manage.py test tests/unit -q

# ✅ 也可直接调用运行器（参数原样透传给 pytest）
.\.venv\Scripts\python.exe scripts\run_tests_in_job.py tests -q -p no:randomly

# ❌ 不要这样：父进程一被强杀就留孤儿
python -m pytest tests/unit -q
```

`scripts/run_tests_in_job.py` 把 pytest 放进一个
**`JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`** 的 Job：Job 句柄只被运行器持有，
运行器一消失（正常退出 / 被 taskkill / 控制台关闭 / 超时终止）→ 内核关闭
该句柄 → **pytest 及其全部后代一并被终止**。

**对照实验**（强杀父进程后看残留）：

| 方式 | 结果 |
|---|---|
| 有 Job 保护 | ✅ 无残留（内核一并终止） |
| 无保护（shell 启动，杀 shell） | ❌ 留下孤儿 pytest |

**排查命令**：

```powershell
# 现在有没有孤儿 pytest（父 PID 已不存在的；CPU 长时间为 0 = 卡死，可直接 Stop-Process）
Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" |
  Where-Object { $_.CommandLine -match 'pytest' } |
  Select-Object ProcessId, ParentProcessId, CreationDate

# 某进程是否在 Job Object 里（判定"Job 关闭陪葬"这条杀因）
.\.venv\Scripts\python.exe scripts\job_status.py 8110
```

**为什么 Job Object 比"轮询父 PID"可靠**：轮询有两个洞 —— 轮询间隔内
父死了它还在跑；以及父 PID 被复用会误判。Job Object 是内核级的，两个洞都没有。

### 2.7 内存与"被强杀"的判据（2026-09-26 实测）

**先说结论：Windows 不会因为物理内存紧张就杀进程。** 它只会把页换出去；
真正触发终止的是**提交量（commit）耗尽**。所以判断"是不是内存杀的"，
要看**提交量**而不是物理内存。

实测当时的状态：

| 指标 | 值 | 判读 |
|---|---|---|
| 物理内存 | 31.7 GB，可用 **3.1 GB（90% 已用）** | 偏紧，但**不足以杀进程** |
| **提交量** | **49.05 / 63.74 GB（77%）**，剩 **14.78 GB** | 离耗尽很远 → **排除 OOM** |
| 页面文件 | D 盘 32 GB（峰值用 8.8 GB），自动管理**关** | 提交量的后盾，够用 |
| 内存大户 | llama-server **9.8 GB**、Trae CN 6.8 GB、WorkBuddy 3.0 GB | Ollama 模型常驻是最大项 |

**"被强杀"的排查顺序**（本次结论：前三条全被排除）：

1. **崩溃？** 看 `data/run/backend.log` 尾部有没有异常/退出记录。
   强杀**不走 lifespan**，所以"日志干净地停在最后一个 200"就是强杀的特征。
2. **OOM？** 看提交量（不是物理内存）。见上表 —— 77% 就排除了。
3. **Job Object 陪葬？** 跑 `scripts/job_status.py <port>`。
   实测结果：**不在任何 Job 里** → 排除。
4. **那就是显式 `taskkill /F`**（或等价的外部强制终止）。

**验证过的保护措施**：

| 措施 | 作用 | 状态 |
|---|---|---|
| `MossPilotWatchdog` 计划任务 | 服务消失后 **≤5 分钟自动拉起**，现场入 `backend_incidents.jsonl` | ✅ 已启用（实测 19:12:43 死 → 19:15:17 恢复） |
| 事故流水带内存现场 | 每次重启记录**当时**的可用内存/提交量/是否在 Job 里 —— 这些是**运行期状态、事后无法回溯**的 | ✅ `manage.py ensure` 已记录 |
| 应用层缓存 + KeepAlive | 隧道抖动时表现为"数据略旧"而非"打不开" | ✅ `panelPrefetch` / `intelCache` / `alertsCache` |

**可选的加固（需你决定，我没动）**：机器物理内存常年在 90%，
最大项是 Ollama 的 **llama-server 9.8 GB**（两个实例）。
若这个模型不常驻使用，把它改成按需加载可以把可用内存抬到 12 GB 以上 ——
虽然**不解决**这次强杀（已排除 OOM），但对 SQLite 与行情任务的稳定性有实际好处。

---

### 2.8 对外实例的自动拉起（值守）

| 项 | 值 |
|---|---|
| 计划任务 | **`MossPilotWatchdog`**（**每 1 分钟**一次；2026-09-27 从 5 分钟收紧 —— 间隔 = 进程消失后**最长不可用时间**，用户侧表现为"间歇性打不开/后端不可达"） |
| 包装脚本 | `scripts/pilot_watchdog.ps1` |
| 实际动作 | `python manage.py ensure --env pilot --port 8110` |
| 死亡现场 | `data/run/backend_incidents.jsonl`（JSONL，含**最后一次活动时刻** + 内存/提交量快照） |
| 值守自身异常 | `data/run/pilot-watchdog.log`（**只在退出码非 0 时**写一行） |

> ⚠️ **一次静默死亡的真实代价**（2026-09-27 复盘）：后端进程会**无声消失**
> （`backend.log` 最后一行仍是 `200 OK`，事件日志无崩溃记录，像被强杀），
> `ensure` 只能"发现端口空了再拉起来"。所以**间隔就是用户看到 502 的时长**：
> 5 分钟 → 最长 5 分钟不可用；1 分钟 → 最长 1 分钟。
> 若再次发生，优先看 `backend_incidents.jsonl` 的 `mem_free_gb/commit_pct`
> 与 `last_activity`，并考虑用"父进程持有 uvicorn + 记录退出码"的守护方式
> 换掉轮询（轮询拿不到退出码，这是它唯一的短板）。
> 详见 `docs/INCIDENT_FIRST_SCREEN_LOOP_BLOCK_20260927.md` §三。

**隧道值守（2026-09-26 新增，同一套思路）**：

| 项 | 值 |
|---|---|
| 计划任务 | **`MossTunnelWatchdog`**（每 5 分钟一次） |
| 包装脚本 | `scripts/tunnel_watchdog.ps1` |
| 实际动作 | `python manage.py tunnel-ensure` |
| 流水 | `data/run/tunnel_incidents.jsonl` |
| 自身异常 | `data/run/tunnel-watchdog.log` |

⚠️ **`tunnel-ensure` 的判据**：只有"连不上 / 超时 / **5xx**"才算失败，
**4xx 一律算成功** —— 能拿到业务错误码就说明请求走完了
cloudflared → 后端 → 回来的整条路。踩过的坑：第一版把任何非 200 当失败，
于是探测路径被登录门槛拦下（401/403）时误判为故障，**白白重启了一次隧道**
（重启会中断所有在途请求）。

```powershell
# 看它有没有在干活 / 最近一次结果
Get-ScheduledTaskInfo -TaskName 'MossPilotWatchdog' |
  Select-Object LastRunTime, LastTaskResult, NextRunTime
Get-ScheduledTaskInfo -TaskName 'MossTunnelWatchdog' |
  Select-Object LastRunTime, LastTaskResult, NextRunTime

# 看历史上"死过几次、死在哪一刻"
Get-Content data\run\backend_incidents.jsonl
Get-Content data\run\tunnel_incidents.jsonl

# 手动跑一次（健康时完全静默、不写任何日志）
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\pilot_watchdog.ps1
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\tunnel_watchdog.ps1
```

**三条必须知道的约束**（`manage.py ensure` 的判据，改之前先读）：

1. **判据只看"端口有没有人听"**，**不**看 `/health`。`/health` 冷启动时会几十秒
   才回（§2.5 与 `cmd_status` 的注释都记过），把"慢"判成"死"会让值守反复重启
   一个正在正常工作的实例 —— 那是自己制造故障。
2. **端口被其他程序占用时拒绝启动**（退出码 2）并记账，**绝不先杀再起**：
   `kill_pid_tree` 是硬杀，会打断别人的业务程序。
3. **绝不 `--replace`**：它按命令行枚举本项目**全部**后端进程，会顺手停掉
   正在跑的另一个实例（dev 8100）。

**⚠️ 改 `.ps1` 必须存成「UTF-8 with BOM」**

Windows PowerShell 5.1 会把**无 BOM** 的 `.ps1` 按 ANSI 解码：文件里的中文
被打乱后会导致**引号失配、整个脚本解析失败**。而包装脚本末尾是 `exit 0`，
于是**计划任务永远报"成功"却什么都没做** —— 值守看起来在跑、其实早就没了，
是那种最难发现的静默失效（2026-09-26 实测踩到）。

带 BOM 重写：

```powershell
$p = 'scripts\pilot_watchdog.ps1'
$t = [IO.File]::ReadAllText($p, [Text.Encoding]::UTF8)
[IO.File]::WriteAllText($p, $t, (New-Object Text.UTF8Encoding($true)))
```

自检：文件前 3 字节应为 `239,187,191`。

### 2.9 ★★ 自启/值守**存在性**自检 + 外部监控（`CHG-0168`，2026-10-05 全站 502 事故）

**事故经过（一句话）**：不是"自启失败"，是**自启任务已经不存在了**。
`MossPilotAutostart` / `MossPilotWatchdog` 两个计划任务被删除，只剩 `MossFrpEnsure`
照常每 5 分钟一跳 —— 它探到"本机后端 8110 返回 000"就按设计**主动放手**
（日志原话：`跳过隧道处置（归 PilotWatchdog）`），而它托付的那个值守**不存在**。
于是日志里写着一句看起来完全正常的话，实际无人负责，**客户先发现打不开**。

**为什么查不到是谁删的**：`Microsoft-Windows-TaskScheduler/Operational` 当时是
**关闭**的（本次已开启）。**结构变更自带静默失效** —— 这是本项目反复出现的形状。

#### ① 唯一事实源 + 一条命令体检

```bash
uv run python scripts/moss_autostart.py --check      # 三态体检（退出码见下）
uv run python scripts/moss_autostart.py --json       # 机器可读
uv run python scripts/moss_autostart.py --install    # 按**同一张表**幂等重建（需人点头）
uv run python scripts/moss_autostart.py --self-test  # 检查器自证（喂已知答案）
```

`REQUIRED_TASKS`（`scripts/moss_autostart.py`）是**唯一事实源**：`--check` 与
`--install` 读同一张表，所以"装了什么"和"查什么"不可能漂移。

| 任务 | 作用 | 间隔 |
|---|---|---|
| `MossPilotAutostart` | 开机（+30s）/登录拉起 pilot 8110 | — |
| `MossPilotWatchdog` | 运行期 `ensure`（全项目唯一运行期值守入口） | 60s |
| `MossFrpEnsure` | hk 公网链路（SSH 隧道 + frpc）自愈 | 300s |
| `MossAutostartGuard` | **核对上表**；不达标发信（本文件存在的理由） | 1800s |

**退出码是三态，不是两态**：`0` OK · `1` 确认坏了（缺失/停用/动作或间隔不符）·
`2` **读不到**（探针故障，**不是"通过"**）。把 `2` 当 `0` 就是假绿；
把"读不到"说成"不存在"则会让人去重建一个本来好好的任务。

> `MossFrpEnsure` 由本表**只核对、不代装**：它的动作必须是 `wscript.exe` +
> 交互式 Administrator 身份（SYSTEM 身份下 `Path.home()` 变了、SSH 密钥找不到、
> 隧道直接起不来 —— 见 `scripts/frp_watchdog.ps1` 头部）。这类承重细节，
> 通用安装器代装只会把它装坏。

#### ② 计划任务审计日志（本次开启，别关回去）

```powershell
wevtutil sl Microsoft-Windows-TaskScheduler/Operational /e:true
wevtutil sl Microsoft-Windows-TaskScheduler/Operational /ms:33554432   # 32 MB
```

开启后可以**区分两种触发**（实测：同一个任务的两次运行）：
`事件 110` = 有人手动触发；**`事件 107` = 时间触发器自己启动**；
`201/102` = 动作完成。看谁在什么时候把任务删掉，也终于有据可查。

#### ③ 外部监控（跑在香港 VPS 上，**不依赖本机**）

本机上的任何脚本都没法告诉自己"我挂了"（整机没开、断网时更是一行都不跑），
所以这一层刻意放在另一台机器上 —— 只看一个外部事实：公网入口还能不能拿到 200。

```bash
ssh -i ~/.ssh/moss_hk_tunnel ubuntu@43.128.5.94 \
  "tail -5 ~/moss-monitor/monitor.log"          # 每 1 分钟一行心跳
ssh -i ~/.ssh/moss_hk_tunnel ubuntu@43.128.5.94 \
  "python3 ~/moss-monitor/moss_vps_monitor.py --status"
```

* 连续失败 **≥ 3 次**才算故障（单次抖动不叫人）；恢复时**也发一封**；
* 每一跳都写流水（**含健康心跳**）—— 否则"在跑且没事"与"早就没了"长得一样；
* 闸门三层（去重/速率/静默）与 `moss_autostart.py` **import 同一份代码**
  （`scripts/moss_ops_alert.py`），判据是"同一个函数对象"，见单测。

**通知通道（默认未配 = 只写日志，会明确记 `notify=unconfigured`，绝不假装通知过）**，
二选一即可激活：

```bash
# A. webhook（凭据面最小，推荐）：写进 ~/moss-monitor/notify.env
MOSS_ALERT_WEBHOOK=https://<你的机器人 webhook>

# B. 邮件（复用既有 QQ 邮箱授权码）：同样写进 notify.env（chmod 600）
ALERT_SMTP_USER=...        # 只写在这个 0600 文件里，不进命令行、不进日志
ALERT_SMTP_AUTH_CODE=...
ALERT_EMAIL_TO=...
```

> ⚠️ 两个监控必须**互相独立**：本机自检看见"任务被删"，VPS 看见"整机/链路挂"。
> 只做其中一个，都会留下另一半盲区。

---

### 2.10 ★★★ 改 venv（`uv sync`）**必须在停服务时做**（`CHG-0237`，2026-10-08 又一次全站 502）

> 本节每条时间都是实测；完整事故分析见 `docs/PRD.md` §41.45。

**现象**：`https://hk.wujiaitool.cn/` 全站 502（nginx），而本机 8100/8110 **都 200**。
坏在「VPS nginx → 上游」那一跳：`frp_ssh_tunnel` 与 `frpc.exe` **一个进程都没有**，
而 `import paramiko` 当场失败：

```
ImportError: cannot import name 'default_backend'
             from 'cryptography.hazmat.backends' (unknown location)
```

★ `(unknown location)` = **命名空间包**（目录在、`__init__.py` 不在）⇒ **这份安装是半装的**，
**不是版本不兼容**。（反证：同版本 wheel 装到临时目录，`default_backend` 可用。）

**根因**：有人在**服务正在运行**时跑了 `uv sync`。带原生 DLL 的三个包
（`cryptography` / `bcrypt` / `lightgbm`）文件被进程锁住，uv 卸不掉也写不完
⇒ `dist-info` 连 `METADATA`/`RECORD` 都没有、包目录被掏空。

**为什么当时没人发现**：**在跑的进程把已 import 的模块留在内存里**。
venv **11:51** 就坏了，隧道一直跑到 **18:19** 那批进程死掉、需要重新 import 时才炸
（18:19 → 22:11 客户可见中断 **3 小时 52 分**）⇒ **半装的 venv 是定时故障，不是立即故障**；
反过来也成立：**修好也必须重启才算生效**（同 §2.3b 的 `models.yaml` 那条）。

**铁律三条**

1. **改 venv 就走「停服务 → sync → 起服务」**；不能停的时候，就不要 sync。
2. ★ **先禁用值守任务再动手**（`MossPilotWatchdog` / `MossFrpEnsure`）：
   它们每 1~5 分钟把服务拉回来，会在你 sync 到一半时**再锁一次**、装出第二份半装。
   修完记得 `schtasks /change /tn <任务名> /enable` 恢复。
3. 包目录被掏空时 `uv sync` **不会**修它（它认为"已安装"，实测只打印
   `Checked 126 packages` 而什么都没做）。必须**按名字删掉那个包目录**再 sync。

**部署/修复命令（必须带 extras）**

```powershell
uv sync --all-extras              # ★ 隧道要的 paramiko 在 tunnel extra 里，漏了就 502
uv sync --all-extras --dry-run    # 判据：期望 "Would make no changes"
```

**两个"承重件"已声明**（`CHG-0237` 之前它们只是"临时装的"，一次 `uv sync` 就会被清掉）

| 包 | 谁在用 | 没声明时的症状 |
|---|---|---|
| `paramiko`（`tunnel` extra） | `scripts/frp_ssh_tunnel.py` | **全站 502** |
| `bcrypt`（core） | `auth_sqlite_repo.verify_password()` | pilot 34 个账号里 **21 个是 `$2b$`**，而校验**按前缀派发**且"任何异常都返回 False" ⇒ 用户看到的是**「密码错误」**（与真输错密码**无法区分**） |
| `lightgbm`（core，`CHG-0238`） | `moss_selector/models/*` 的 **joblib 反序列化** | ★ **静态扫 `import` 扫不到它**（源码里没有那一行）⇒ 症状不是报错，而是「`moss_selector/models` 下没有可用的模型文件」+ 反复自动重训（模型其实一直都在）。**`manage.py` 自己的依赖自检会拒绝启动并说清原因** —— 那条自检是对的 |

> ★ **「没 import」≠「不需要」**：判据要用"真实依赖"（跑一次 / 启动自检），
> 不要用"静态扫 import"。`lightgbm` 就是被这条判据漏掉的（我一度把它写成"孤儿包"，
> 见 `docs/PRD.md` §41.45.7 的废止痕迹）。

**修单个实例时：用 `start`，不要用 `restart-*` 或 `--replace`**（`CHG-0238` 实测踩过）

```powershell
# ✅ 只启动，不停别人（本次事故里我把 pilot 误杀的教训）
uv run python manage.py start --env pilot --port 8110 --daemon
# ❌ restart-pilot 的停止段**按 PID 文件杀**，而 PID 文件不按 env 分
#    ⇒ `restart-pilot --env dev --port 8100` 会把 pilot + 它的 worker 一起杀掉
# ❌ start --replace 按命令行枚举本项目**全部**后端进程
```

**起隧道要用分离进程**（不要把隧道挂在一个"会挂住的包装命令"底下 ——
那样 kill 那个命令会把隧道和 frpc 一起带走）：

```powershell
Start-Process .venv\Scripts\python.exe -ArgumentList 'scripts\frp_ssh_tunnel.py' -WindowStyle Hidden
Start-Process bin\frpc.exe -ArgumentList '-c','frpc.toml' -WindowStyle Hidden
```

**健康判据**

```powershell
.venv\Scripts\python.exe -c "import bcrypt,paramiko,cryptography,nacl; from cryptography.hazmat.backends import default_backend; print('ok')"
powershell -File scripts\frpc_start.ps1
curl.exe -s -o NUL -w "%{http_code}`n" https://hk.wujiaitool.cn/     # 期望 200
```

---

## 3. 隧道（cloudflared）

```powershell
# 进程在不在
Get-Process cloudflared


# 配置（回源目标在这里）
Get-Content "C:\Windows\System32\config\systemprofile\.cloudflared\config.yml"
```

**关键事实**：隧道把 `moss.wujiaitool.cn` 回源到 **`127.0.0.1:8110`**（不是 8100）。
所以**对外服务是 8110 那个实例** —— 改 8100 不影响公网。

> **踩坑记录**：曾把 8110 的实例误判为"僵尸进程"并停掉，导致公网 502。
> **教训：动生产进程前先读隧道配置，不要靠"我测不通"推断。**

### 3.1 ⚠️ 已知瓶颈：隧道本身慢且会挂死（2026-09-26 实测）

这一节是**换隧道方案的决策依据**，别重复试。

**同一条 `/api/v1/health/live`，同一时刻的对照**（这条对照是整节的核心）：

| 路径 | 结果 |
|---|---|
| **后端本机** | **1.9 ~ 3.4 毫秒，5/5 成功** |
| **走公网隧道** | **1.14 ~ 11.5 秒，9/10 成功（1 次直接挂死）** |

**后端是 2 毫秒级的健康服务，问题全在隧道。** 用户看到"后台挂了"，
实际挂在隧道上 —— 所以**别急着去查后端**，先照这张表对照一次。

**★ 重启隧道能恢复**（实测两次，同一测法）：

| | 重启前 | 重启后 |
|---|---|---|
| 成功率 | 9/10（1 次挂死） | **10/10** |
| 中位延迟 | 3.34 s | **1.60 s** |
| 最大延迟 | 11.5 s | 8.94 s |

这条链路**会随时间劣化**：连续跑 35 小时后劣化、另一次 23 分钟后复现同样形态。
所以已做成自动的 —— 见 §2.8 的 `MossTunnelWatchdog`。

**排除过的两条错误假设**（都靠实测，别再走一遍）：

1. **不是线路质量**：ping CF 边缘 **159 ms、0% 丢包**（min 160 / max 159，极稳）。
2. **不是 cloudflared 连接老化**：把进程重启（当时已跑 35.2 小时）后，
   仍然 15 秒超时 + 1.5~3.3 秒延迟 —— 但**重启确实改善了成功率与中位延迟**
   （见上表）。两者不矛盾：重启清掉的是**劣化的连接池**，不是线路本身。

**结论**：**部分 HTTP 流会永久挂住**（走到一半没有任何超时/重传救援）。
这是 CF 免费隧道这条路径的固有行为，应用层改不动它。

**已排除的替代方案**（都不需要改代码就能试）：

| 方案 | 实测 | 可用性 |
|---|---|---|
| **localtunnel**（`npx localtunnel --port 8110`） | 热连接 ttfb **稳定 1.34~1.42 s**（约比 CF 快 2 倍） | ❌ **不可用**：浏览器访问会被插 interstitial 页（要访问者公网 IP 当密码），对真实网站是死的 |
| **serveo.net**（SSH 反向隧道） | —— | ❌ **已停用**：`Permission denied (publickey,keyboard-interactive)`，现在要 SSH 公钥 |
| pinggy.io | 未测 | ⚠️ 免费版需临时 SSH 公钥，且会话有**小时级时长上限**，不适合长期站点 |
| ngrok | 未测 | ⚠️ 需注册拿 authtoken（没法无人值守开通）；免费版访问页有提示页 + 域名随机 |
| **frp / rathole + 国内云主机** | 未测 | ✅ **推荐**：自建完全可控、无提示页、国内中转延迟**数量级**改善。代价是要一台 VPS（最低配即可） |

**当前应对**（已落地，不依赖换隧道）：

- 前端把"惰性"当一等公民：`panelPrefetch` + `KeepAlive` + `alertsCache`/`intelCache`
  → **切页签与二次访问 ≈ 0 网络**，隧道再差也不影响这些路径。
- `/intel/bootstrap` 把首屏 2 条请求并成 1 条（每次往返都有一笔固定开销）。
- `IntelPanel` 的 `LOAD_TIMEOUT_MS = 6000`：超时即用缓存渲染并解除忙碌态，
  让抖动表现为"数据略旧"而不是"转圈几十秒"。
- `MossTunnelWatchdog`（每 5 分钟）：连续探测失败就自动重启 cloudflared。

---

### 3.2 ⚠️ 隧道值守的两个坑（2026-09-26 实测，都已修复）

**坑 ①：`cloudflared` 是 Windows 服务，不能自己去杀+起**

这台机器上它是**以服务方式运行**的，生命周期归 SCM 管：

```
Cloudflared 服务   StartMode = Auto
SERVICE_START_NAME = LocalSystem
FAILURE_ACTIONS    = RESTART -- Delay = 20000 milliseconds   ← SCM 会自动拉起来
```

原来的重启逻辑是"`taskkill` 全部 cloudflared + 自己 `Start-Process` 一个"，
于是和 SCM 的自动恢复打架：

```
杀掉服务进程 → SCM 20 秒后拉起一个（PID A）
            → 我们又起一个（PID B）＝ 两个实例
```

**症状**：`tunnel info` 里出现**两个 connector**（实测创建时间相差 16 秒），
互相抢同一个隧道；而流水里连续两次 `restart_ineffective`
（**重启不但没修好，重启本身成了不稳定源**）。

**修法**：改走服务 —— `Restart-Service -Name Cloudflared`（在跑但不通）
或 `Start-Service`（停了）。由 SCM 保证永远只有一个实例。

**排查命令**：

```powershell
# 服务状态与恢复策略
Get-CimInstance Win32_Service -Filter "Name = 'Cloudflared'" |
  Select-Object State, StartMode, ProcessId
sc.exe qfailure Cloudflared

# 有没有重复实例（应只有 1 个，且 PID == 服务的 ProcessId）
Get-CimInstance Win32_Process -Filter "Name = 'cloudflared.exe'" |
  Select-Object ProcessId, CreationDate
```

#### ★ 更深一层的教训：**我们自己就是"服务意外终止"的原因**

`Get-WinEvent` 里 SCM 会记 `7031 服务意外地终止`。查它的历史会发现：

```
09/25 00:29 ~ 00:43   计数一路涨到 28 次   ← 曾有一次真正的崩溃循环
（之后一直稳定，没有人动它）
09/25 08:34:57        第 1 次              ← 稳定期的孤立一次
09/26 19:47:01        第 1 次              ← ★ 我开始手工重启隧道
09/26 20:12:15        第 2 次              ← ★ 我的 tunnel-ensure 在杀进程
09/26 20:14:16        第 3 次              ← ★ 同上
09/26 21:56:56        第 4 次              ← ★ 同上
```

**新增的那 4 次崩溃全部由我的 `taskkill` 造成。**

所以当时的推理链是错的：我拿"服务意外终止"的日志去证明"它不稳定"，
而那些终止**正是我自己造成的**。**在拿日志当证据之前，先确认那段时间
自己有没有动过它** —— 否则会把自己的动作当成系统的病症。

**坑 ②：探测窗口太短，一次抖动就误重启**

CF 这条路径会有 **15~30 秒的瞬时超时**（同一时段实测到 8.3 秒、15.9 秒各一次），
而第一版探测是 3 次 × 2 秒（总窗口 12 秒）——**一次抖动就被判成故障**，
触发了一次不必要的重启。

现在改成 **4 次 × 5 秒（约 60 秒窗口）**：跨得过瞬时抖动，
真故障也能在一分钟内恢复。每次探测的实际延迟/错误都记进流水
（`probe_log`），事后能区分"4 次都超时"与"4 次都很快但 500"。

**实测验证了这次调整的必要性**（`--verbose`）：

```
[tunnel] 第 1 次失败：TimeoutError（20531ms）   ← 20.5 秒超时
[tunnel] 第 2 次探测成功（HTTP 403 属 4xx）      ← 但它其实通
  退出码=0  总耗时=32.1s
```

按旧参数，这一次就会误判并重启一根**没坏**的隧道。

> **这本身也说明**：CF 隧道不能作为正式入口。换香港（`docs/HK_VPS_MIGRATION.md`）
> 的直接理由就是它 —— 不是"配置不对"，而是这条路径**固有地会瞬时挂死**。

---

## 4. 账号与密码

### 4.1 看账号

```powershell
.\.venv\Scripts\python.exe scripts\reset_admin_password.py `
    --list --db data/pilot/moss_pilot.db
```

### 4.2 重置密码（推荐写法）

```powershell
.\.venv\Scripts\python.exe scripts\reset_admin_password.py `
    --username admin `
    --db data/pilot/moss_pilot.db `
    --expect-db pilot `
    --clip
```

| 参数 | 作用 |
|---|---|
| `--username` | 要重置的账号 |
| `--db` | **必填**，否则读错库（§1） |
| `--expect-db pilot` | 保险：路径不含 `pilot` 就拒绝执行 |
| `--clip` | 新密码进剪贴板（实测 `# $ & !` 全部保真） |
| `--one-line` | 打成一行，防终端折行抄错 |
| `--keep-must-change` | 保持"首登强制改密"（默认关，避免再次登不上） |

**重置的影响范围**（已实测）：

- ✅ 只改 `dim_user_credential` 里**那一行**
- ✅ 只吊销 `fact_session` 里**该用户**的会话（`WHERE user_id = ?`，是 `UPDATE` 打标记，不删行）
- ✅ 认证表之间**无外键** → 不存在级联删除
- ✅ 业务数据（`fact_data_points` 138 万行等）**完全不碰**
- ⚠️ 其他用户**不受影响**

**密码是单向哈希**（argon2id > bcrypt > pbkdf2_sha256 60 万次迭代 + 随机盐），
**原文不可恢复** —— 只能重置。这是正确设计。

### 4.3 网页找回密码

```
1. https://moss.wujiaitool.cn/  → 「忘记密码」
2. 填邮箱 → 过图形码 → 发验证码
3. 邮件里应有【验证码】+【重置令牌】两行
4. 页面两个框都填 + 新密码 → 提交
```

> **历史故障（已修）**：曾经邮件只发验证码、令牌从未签发，
> `fact_password_reset` 表恒为 0 行 → `/password/reset` 恒定 401。
> 修复见 §8「找回密码链路修复」。

### 4.4 新建/提升管理员

```powershell
# 新建（会打印一次初始密码，首登强制改密）
.\.venv\Scripts\python.exe scripts\bootstrap_admin.py `
    --username admin2 --email you@example.com --db data/pilot/moss_pilot.db

# 把已有用户提升为管理员
.\.venv\Scripts\python.exe scripts\bootstrap_admin.py `
    --promote someone --db data/pilot/moss_pilot.db

# 列出管理员
.\.venv\Scripts\python.exe scripts\bootstrap_admin.py `
    --list --db data/pilot/moss_pilot.db
```

> ⚠️ `bootstrap_admin.py` **不能重置已有账号的密码**（对已存在用户名直接退出）。
> 重置用 §4.2。

### 4.5 存密码的三种方式（按可靠性排序）

| 方式 | 说明 |
|---|---|
| **Edge 自带**（最省事） | 登录时弹「保存密码」点保存；查看：`edge://settings/passwords` |
| **纸笔 + 锁起来**（最抗灾） | 唯一在"硬盘挂了/系统重装"时还救得了你的方式 |
| **装 KeePassXC** | 开源免费、本地存库、不联网 |

**为什么不写文件**：明文长期落盘，备份/截图/打包发人时一起走。
剪贴板用完即走，是更合适的取舍。

---

## 5. 测试与体检

```powershell
# 隔离环境跑测试（可在线服务运行时执行，对线上零影响）
.\.venv\Scripts\python.exe manage.py test -q
.\.venv\Scripts\python.exe manage.py test -k "auth or notify"   # 只跑相关

# SQLite 体检与自愈
.\.venv\Scripts\python.exe manage.py doctor

# 前端构建
.\.venv\Scripts\python.exe manage.py build
```

> **为什么必须用 `manage.py test`**：它把 SQLite / LLM 审计 / 调度记录 / 缓存
> 全部重定向到一次性临时目录，**线上服务可同时运行**。

### 数据保留与库体积（2026-09-25 审计后新增）

**清理是自动的**：`data_retention_daily`（每日 03:30）+ 应用启动后 120 秒各跑一次
`retention_service.run_retention()`。三档：`fact_data_points`（按年）、
`news_cache`（按天）、**认证/告警/通知流水**（按天，见 `.env.example` 的
`RETENTION_*`）。

**默认只清"过了保留期就毫无价值"的流水表**。历史行情台账
（`sector_crowding_daily` / `auction_*` / `fact_quant_selection`）默认
**不清理**（`RETENTION_* = 0`）—— 它们原本是有意永久保留的，删了不可恢复。
要控制体积再显式设保留期。

```powershell
# ★ 回收空闲页：清理删了行，但 auto_vacuum=0 → 文件永不缩小
# 必须先停服；VACUUM 会重写整个库并持有排他锁（6GB 级库是分钟级）
.\.venv\Scripts\python.exe manage.py stop
.\.venv\Scripts\python.exe manage.py vacuum --yes
.\.venv\Scripts\python.exe manage.py start
```

> **为什么 `vacuum` 是手动命令而不是定时作业**：它需要排他锁 +
> 约等于库大小的临时空间，是唯一会让在线服务整段不可用的操作。
>
> **实测背景**（2026-09-25）：主库 6.32 GB 中 **3.96 GB（63%）是空闲页**
> —— 保留清理删掉了行、页被标记空闲，但文件不缩。清理解决"行数膨胀"，
> VACUUM 才解决"文件膨胀"，两者缺一不可。

**测试进程里清理会被自动拒绝**（`retention_passes.destructive_allowed` 检测
`PYTEST_CURRENT_TEST`）。这不是多此一举：`TestClient(app)` 会触发启动钩子，
而 `main.py` 的 `settings` 是模块导入时求值的，测试里的 `MOSS_SQLITE_PATH`
隔离**对它无效** —— 清理会打到真实库。本仓已因同一陷阱清空过一次生产认证表。
要测清理逻辑本身，用临时库并设 `MOSS_RETENTION_IN_TEST=1`。

### 已知的 8 个失败（**非认证相关，先前遗留**）

```
test_alert_thresholds.py (5)   test_alert_models.py (1)
test_alert_api.py (1)          test_notifiers.py (1)
test_env_guard.py (1)          test_sell_points.py (1)
```

**根因**：告警**评分门控未生效** —— 测试要求 `risk_score=69` 被抑制（阈值 70），实际 `sent`。
对应工区里 `.trae/specs/event-alert-system/` 的未提交改动（告警功能未收尾）。

**判据**：失败**全在 `alerts` 子系统**，与认证无关。

---

## 6. 前端

```powershell
# 只起前端 dev server（5173，/api 代理到 8100）
.\.venv\Scripts\python.exe manage.py frontend

# 构建生产包（产物 web/dist/，后端自动托管，单端口即可）
.\.venv\Scripts\python.exe manage.py build

# ★ 发布到对外实例：web/dist → web/dist-pilot（8110 只读这一份）
.\.venv\Scripts\python.exe manage.py ship-frontend
```

**`ship-frontend` 不能省**：pilot 的 `MOSS_WEB_DIST` 指向 `web/dist-pilot`，
`web/dist` 是 dev（8100）在用的。只跑 `build` 不 ship，**客户刷新看到的还是旧界面**。
ship 之后无需重启后端（`StaticFiles` 每次请求都读盘）。

### 6.1 开机探测为什么只有一次往返（`/auth/bootstrap`）

前端"正在检查登录状态…"背后只发 **1 个**请求：`GET /api/v1/auth/bootstrap`。

以前是 3 次串行（`/me` 401 → `/refresh` → `/me` 200）。本机每次 1~62 ms 无所谓，
但公网单次实测 **0.4~2.0 秒且偶发 502**，三次叠起来就是"停十秒"。
其中第三次是纯浪费：`/refresh` 的响应里本来就带 `user`。

新端点把"先看会话、确实没有才续期"搬到服务端，所以：

- 顺序约束（**绝不能无条件先 refresh**，否则令牌轮换被网络抖动吞掉会触发
  重放检测、撤销整条令牌家族）由服务端保证，客户端无法用错顺序；
- `authenticated:false` 是**正常结论**而非错误码（返回 200），
  前端不用异常控制流判断登录态。

**排障就看控制台**，探测结束会打一行：

```
[auth] 开机探测 续期成功：680 ms（/auth/bootstrap 单次往返）
[auth] 开机探测 已有会话：210 ms（/auth/bootstrap 单次往返）
```

这行直接就是用户盯着转圈的时间。若它很小但界面仍慢，说明瓶颈**不在认证**，
去查别的接口；若它几百毫秒以上，看 §7 的 502/隧道条目。

---

## 7. 常见故障速查

| 现象 | 先查什么 |
|---|---|
| 公网 502 | ① `Get-NetTCPConnection -LocalPort 8110` ② §2.1 重启 |
| 网页能开但登录说账号不存在 | **读错库**（§1）—— 查 `backend.log` 的 `认证服务已装配` 行 |
| 重启后前端几十秒没数据 | 孤儿进程占 `-wal`/`-shm` → `manage.py doctor` |
| 脚本 `--list` 显示 0 个账号 | **读错库**（§1）—— 补 `--db data/pilot/moss_pilot.db` |
| 库文件只涨不缩 | 清理解决行数、**VACUUM 才解决文件**（§5「数据保留与库体积」） |
| 日志里 `database is locked` | 写并发争锁。历史 95 次已由 `busy_timeout` 修复；若复现查是否有长事务批量 INSERT（如 `macro_repo` 的逐行写） |
| 邮件收不到验证码 | 看 `fact_notify_log` 的 `status`/`provider`；`console` = SMTP 没配 |
| 日志刷 `飞书 Ip Not Allowed` | **可忽略** —— 飞书通道只留了接口未接入，与业务无关 |
| 数据库 `disk I/O error` | `manage.py doctor`（伴生文件自愈） |
| 刷新页面一直转"正在检查登录状态" | 看浏览器控制台的 `[auth] 开机探测 …ms`（§6.1）。**不会**卡在 checking 了：探测失败会明确回登录页并说明原因。慢则往隧道/502 方向查 |
| 改了前端但客户看不到 | 忘了 `manage.py ship-frontend`（§6）：pilot 读的是 `web/dist-pilot` |

---

## 8. 本次会话改了什么（留档）

### 8.1 登录首屏从"3 次串行公网往返"降到 1 次（2026-09-26 报障）

**现象**：刷新界面一直显示"正在检查登录状态…"，约 10 秒。

**量出来的根因**（不是后端慢）：

| 环节 | 实测 |
|---|---|
| 后端本机 `/auth/me` | 62 ms；`/auth/refresh` 4 ms；登录闸门 DB 查询 0.0 ms |
| 公网单次往返（隧道） | **0.4~2.0 秒**，且有 502；`host` 到 CF 边缘 158 ms 稳 |
| 冷启动串行往返数 | **3 次**：`/me`(401) → `/refresh`(200) → `/me`(200) |

10 秒 = 3 次串行 × 慢隧道，再叠加网络抖动时 `pingServer`（3 秒超时）
被反复触发（访问日志里 `health/live` 连发七八次）。

**改法**：

| 文件 | 改动 |
|---|---|
| `src/api/routes/auth.py` | **新增 `GET /auth/bootstrap`**：会话有效→直接返回身份（**不轮换令牌**）；否则用 remember cookie 静默续期并一并返回身份 + `login_mode`。抽出 `_me_payload()` / `_login_mode_payload()` 供 `/me`、`/auth/login-mode` 共用 |
| `web/src/hooks/useAuth.ts` | 三步探测 → **单次 `authApi.bootstrap()`**；登录时不再白打一次 `/me`；探测**失败**也明确落到登录页（原本会永远停在 `checking`） |
| `web/src/api.ts` | `pingServer` 并发合并 + 2.5 秒缓存 + `navigator.onLine` 短路；`BootstrapResult` 类型 |
| `web/src/components/LoginScreen.tsx` | 复用 bootstrap 带回的 `loginMode`，不再单独问一次 |
| `tests/unit/test_auth_routes.py` | +6 个 bootstrap 用例（含"有会话时绝不轮换令牌""只读模式真的零写入"） |

**顺序约束没有消失，只是搬到了服务端**：`refresh` 会轮换 remember token，
所以绝不能无条件先 refresh —— 响应一丢，旧令牌就变"重放"，服务端会撤销
整条令牌家族把用户踢出去（§8.6.5④）。现在由 `/auth/bootstrap` 保证顺序。

**验证**：全量 **4758 passed / 9 failed**（9 个全是遗留：`.env` 里
`alert_confidence_min=0.6` 与基线断言的 `0.7` 不符，与认证无关）。
前端 `npm run build` exit 0 并已 `ship-frontend`。

### 8.2 更早的改动（同会话前半段）

| 文件 | 改动 |
|---|---|
| `src/domain/auth/service.py` | **找回密码签发一次性令牌**（原来 `issue_reset` 零调用 → 用户永远拿不到令牌 → `/password/reset` 恒 401）；发送失败时一并作废令牌 |
| `src/infrastructure/notify/__init__.py` | `SCENE_RESET` 模板加 `{reset_token}`；**同步 `render()` 兜底白名单**（不同步会让所有非 reset 场景邮件整封发不出） |
| `src/infrastructure/repositories/auth_sqlite_repo.py` | 新增 `discard_reset` / `a_discard_reset` |
| `scripts/reset_admin_password.py` | **新增**：重置已有账号密码的引导入口（`--clip` / `--one-line` / `--expect-db`） |
| `scripts/_list_admins_ro.py` | **新增**：只读列出各库管理员 |

### 8.3 ⚠️ 三处会反复浪费时间的坑（已确认）

1. **全量测试会挂在 WebSocket 用例上**：
   `tests/integration/test_alert_api.py::test_ws_receives_alert_pushed_during_scan`
   里的 `ws.receive_json()` **没有超时**，扫单不推送就永久阻塞
   （实测无任何 CPU 增长）。跑全量请加：
   `--deselect tests/integration/test_alert_api.py::test_ws_receives_alert_pushed_during_scan`
2. **`pytest-randomly` 已装**：不加 `-p no:randomly` 每次顺序都变，
   "挂在第 N 个用例"这类定位会失效。
3. **★ 别直接调 pytest，用 `manage.py test`** —— 否则会留下**孤儿进程**
   （见 §2.7）。

### 8.4 「热点&研报小作文」首屏 5~10 秒（2026-09-26）

**先量，别猜。** 用真实会话打本机 8110：

| 端点 | 改前 | 改后（命中缓存） |
|---|---|---|
| `/intel/feed?limit=60` | 36 ms | 19~33 ms |
| **`/intel/calendar?horizon_days=45`** | **11,133 ms**（675 KB） | **30~59 ms** |
| **`/intel/heat`** | **3,472 ms** | **15~39 ms** |

日历 11 秒的**逐源构成**：宏观 **8,731 ms**、财报 2,402 ms、解禁 1,137 ms、
交易日 852 ms。宏观占八成是因为它**按天逐个请求**财经日历
（`calendar.py` 的 `FEED_FETCH_MAX_DAYS=30` → 30 次 HTTP × ~250 ms）。

**两个独立缺陷叠在一起**：

1. **服务端零缓存**：日历/热度每次请求都重新聚合，而这两类内容
   一天之内几乎不变（休市日是交易所规则、解禁/财报是既定日程）。
2. **前端把整页绑在最慢那一路**：`await Promise.allSettled([feed, calendar])`
   之后才 `setLoading(false)` —— 内容 36 毫秒就到了，用户却要盯着
   「正在读取日程…」看十几秒，**切到日历页签还会重挂面板、重付这 11 秒**。

**改法**：

| 位置 | 改动 |
|---|---|
| `src/domain/intel/prewarm.py` | 新增**慢聚合落盘缓存**（`save_slow`/`load_slow`/`SlowCache`，小时级 TTL）+ `prewarm_slow()` + `prime_slow()` |
| `src/api/routes/intel.py` | `/calendar`、`/heat` 改走 `_slow_payload`：**命中→零上游；过期→先返回旧数据+后台续期；未命中→现拉并落盘**。单飞防连点打上游 |
| `web/src/components/intel/IntelPanel.tsx` | 两路**解耦**：feed 一到就解除 loading；日历独立加载（`loadingCal`），只影响自己那一栏 |
| `src/core/config.py` | `INTEL_SLOW_CACHE_HOURS`（默认 6） |
| `tests/conftest.py` | 隔离 `DEFAULT_SLOW_DIR` —— 否则测试的假数据会写进生产缓存，下次启动被当真实日程读出来（与 `feed_payload.json` 同型事故） |

**为什么过期时给旧数据而不是"空壳 + 后台重建"**（与 `/feed` 的策略不同）：
日历空着没有意义（用户来看的就是日程），而 11 秒的空窗太长。
一份几小时前的日程晚几分钟毫无影响，打开等十秒是用户直接投诉的问题。

**验证**：缓存跨进程重启存活；启动日志会当场报状态 ——

```
情报慢聚合缓存已就绪，首屏无需现拉：calendar_45=新鲜(24.1s)、heat=新鲜(20.1s)
```

若这行显示"未就绪"，说明首屏会现拉一次（首次部署的正常现象，后台预热会补上）。
想手动捂热一次：带会话打一遍 `/api/v1/intel/calendar?horizon_days=45` 与 `/api/v1/intel/heat`。

### 8.5 「事件告警」首次打开 2~3 秒 / 切回再等 2 秒（2026-09-26）

**先量，结论与直觉相反**：

| 环节 | 实测 |
|---|---|
| 后端本机 `/alerts?limit=100` | **28~74 ms**（95 行、索引齐全） |
| 同一条走**公网域名** | **5,705 ms**（第二次直接读超时） |
| 隧道带宽（834 KB 静态包 16.3 s） | **≈ 51 KB/s**（本机同一文件 10 ms） |
| 载荷 | **112,520 B**，95 条 ≈ 1,184 B/条 |

**瓶颈既不是数据库也不是后端，而是把 112 KB 挪过隧道。**
另有一次劣化到 ~4.6 KB/s，同一请求要 20 秒 —— 所以"多传 1 KB"在这里
是几毫秒，但"多传 100 KB"就是好几秒。

**四层改动**（每层都对应上面某一项）：

| 位置 | 改动 | 省的 |
|---|---|---|
| `src/api/routes/alerts.py` | 新增 `alert_to_public(for_list=True)`：砍掉**每条重复的 91 B disclaimer**（95 条 = 8,645 B，占 7.7%）与内部键 `alert_key`/`content_key`/`tenant_id`；滤掉 `affected_stocks` 里三字段全空的占位项 | 112,520 → **91,351 B（↓18.8%）** |
| 同上 | `/alerts` 结果按 (租户,用户,是否管理员,筛选,上限) 做 **45 秒进程内缓存**；`mark_read`/`read_all`/**扫描完成**主动失效 | 切界面/来回点筛选不再重查 |
| 同上 | 新增 `GET /alerts/bootstrap`：列表 + 设置**一次往返**取齐 | 首屏少一次往返 |
| `web/src/alertsCache.ts`（新） | 列表结果落 **localStorage**（5 分钟 TTL）+ **登录成功后预加载** + 登出清空 | 切回来**不再走网络** |

**为什么列表缓存 TTL 只有 45 秒**（而日历是 6 小时）：告警是时效性内容，
缓存久了会出现"我刚点的已读又变回未读"。所以写操作**主动失效**，不靠 TTL 兜。

**为什么前端切回来先画缓存再核对**（stale-while-revalidate）：用户对"切回来"的
期待是**立刻**；缓存刚在手里，没理由扔掉重走 2 秒的隧道。配套的两道保险：
① 缓存 5 分钟过期就不再糊弄；② 新告警到达时 WebSocket 会推 `alert` 通知
（`incomingTick` 变化即触发面板重载），所以**实时性不依赖这份缓存**。

**⚠️ 三个容易踩的地方**：

1. **`/alerts/bootstrap` 必须注册在 `/alerts/{alert_id}` 之前**。FastAPI 按注册
   顺序匹配，否则 "bootstrap" 会被当成告警 ID 吞掉、端点永远 404 ——
   而那个症状看起来像"这条告警不存在"，与路由顺序完全联系不起来。
2. **列表缓存键必须含 `is_admin`**。管理员响应保留 `source_name`/`source_url`，
   与普通用户不是同一份内容；不含它会把管理员的响应发给普通用户 = **数据源泄漏**。
3. **别顺手把 `description`/`affected_stocks` 也从列表裁掉**。`AlertsPanel`
   的 `selectAlert` 是**直接拿列表对象**打开详情抽屉、不重新请求的 ——
   裁了能再省 ~35 KB，代价是"点一条先转两秒"。

**不需要做的**：源站 gzip。实测 Cloudflare 边缘对 JS 与 JSON **都已代压**
（`Content-Encoding: br`），源站再加一层只是白耗 CPU。

**验证**：`tests/integration/test_alert_cache.py`（11 个用例，钉住"裁剪只裁没用的"
"已读必须立刻失效""管理员与普通用户不共用缓存""bootstrap 真能路由到"）。

### 8.6 「热点&研报小作文」加多/空过滤选择器（2026-09-26）
**用户口径**：

> "intel-controls 表头要加一个 多/空 的过滤选择器（多还是空 是模型分析
>   出来的 每条信息第一个字【空】【多】）"

数据本来就存在（`tone.tone` ="偏多"/"偏空"，列表标题前的【多】/【空】就是它），
缺的只是筛选入口。**难点全在"口径一致"上**：

| 位置 | 改动 |
|---|---|
| `src/api/routes/intel.py` | `/feed` 新增 `direction=all/bull/bear`；非法值 **422**（不静默按 all）；`direction` 进缓存键 |
| `src/domain/intel/service.py` | `build_feed(direction=...)` 筛选 + `direction_dist` 角标 + `direction_hidden`；新增 `direction_of()` |
| `src/domain/intel/alert_bridge.py` | `_has_direction` 升格为**全局唯一判据**（见下） |
| `web/.../IntelFeedTab.tsx` | 表头新增方向选择器（【多】绿 / 【空】红，与列表标记同色）；`IntelPanel` 串 `direction` state |

**三个刻意的设计决定**：

1. **多空与可信度是两个正交的轴**，所以是**两个独立参数**而不是并进 `FILTER`。
   并进去的话，用户在「高可信」档下切到【多】就会丢掉可信度档位，
   "高可信 + 偏多"表达不出来。
2. **角标统计在筛选之前**（`direction_dist`）：选中【多】之后【空】的数字不能变 0，
   否则用户切不回去。同理它统计在**可信度筛选之后** —— 切到「高可信」时
   数字要跟着变小，不然点进去会发现"说好的 30 条只有 4 条"。
3. **收容组不参与多空筛选**：它装的是"给不出方向"的条目，筛【多】时整组不出现。

**实测（pilot 真实数据）**：

```
direction=all    items=60  dist={bull:37, bear:2} hidden=0
direction=bull   items=37  ← 37 条全部带【多】标记
direction=bear   items=2   ← 2 条全部带【空】标记
```

**⚠️ 排障：改动后第一次请求可能看不到新字段**

情报流是"立即返回缓存 + 后台重建"。改完代码重启后，**第一个请求拿到的仍是
热加载进来的旧 payload**（缺新字段），要等后台那次重建完成才更新。
**不要把"新字段是 undefined"当成后端没改对。**

实测一遍完整周期（本机 8110，真实会话）：

```
status            → building=False  seq=2  age=14.5s  ttl=60s
等 11s 越过 floor
feed?refresh=true → HTTP 200  请求耗时 57 ms  refreshing=True  age=25.6s
                    ↑ 请求**自己不等采集**，它只是把重建挂到后台
status 轮询        → building=True  持续约 3.0 秒
                    building=False seq=2 → 3   ← 重建完成
feed              → seq=3  refreshing=False  age=0.1s
```

所以：

| 问题 | 答案 |
|---|---|
| `refresh=true` 那次**请求**要等多久？ | **不等**。本机 29~70 ms；公网再叠加 ~200 KB 的传输（隧道慢时 1~3 秒） |
| 后台重建要多久？ | 实测 **约 3 秒**（六源 + 知识星球增量，网络为主） |
| 必须等它吗？ | **不必**。前端会自己收敛：重建完 WS 推 `intel_feed` → 面板自动补拉 |
| 想手动确认 | 轮询 `/feed/status`，等 `building` 由 true 变 false（约 3 秒，建议 0.25~1 秒一次） |

**两个会误判的地方**：

1. **`FEED_REFRESH_FLOOR = 10` 秒**：缓存比这更新时，`refresh=true`
   **根本不触发重建**（直接复用）。所以"带着 refresh 打一次"必须**距上次
   重建超过 10 秒**才有意义 —— 刚建好就再打一次，你会看到 `seq` 纹丝不动，
   以为刷新失效了。
2. **别用"看到 `building=false` 就收工"**：后台任务的置位与请求返回之间有
   竞态，紧接着 `status` 可能仍读到 `building=false`。**可靠判据是 `seq`**
   （只有成功重建才 +1）。要么按上表等 3 秒再看 `seq`，要么看
   `building` 是否出现过 true。

**⚠️ 三方判据已收敛成一份**

前端那个【多】/【空】标记、这次的多空筛选器、事件告警引擎，原来各自实现了一份
"什么叫明确方向"—— 而它们**并不一致**（`service` 那份只看 `has_tone`，
漏了 `neutral` 压制与老存储行兜底）。这种不一致不会报错，只会表现为
"筛了【多】却漏掉几条明显带【多】的"。

现在 `service._has_direction` 与 `service.direction_of` 都**委托**
`alert_bridge._has_direction`。**要改判据就改那一处**，不要再复制。

**验证**：`tests/unit/test_intel_direction_filter.py`（13 个用例，含
"neutral 压过字面值""老行按字面值兜底""service 与 alert_bridge 同进同出"
"缓存键含 direction 且默认值等于显式 all"）。

---

## 9. 环境事实（备忘）

| 项 | 值 |
|---|---|
| 公网 | `https://moss.wujiaitool.cn/` （Cloudflare 代理） |
| 后端监听 | `127.0.0.1:8110`（pilot，对外）；`127.0.0.1:8100`（dev） |
| 隧道配置 | `C:\Windows\System32\config\systemprofile\.cloudflared\config.yml` |
| 启动包装 | `scripts/pilot_autostart.ps1`（开机自启 + 注入 `.env`） |
| 开机自启管理 | 根目录 `日常运维命令删除开机自启.bat` |
| 生产库 | `data/pilot/moss_pilot.db` |
| 邮件通道 | QQ 邮箱 SMTP（`.env` 的 `ALERT_SMTP_*`） |
| 本地模型 | Ollama `127.0.0.1:11434` |
| 其他依赖 | MySQL `3306` · PostgreSQL `5432` · memurai(Redis) `6379` |

---

## 10. 一条铁律

> **动生产进程之前，先读配置（隧道 / 启动脚本 / 服务定义）。
> 不要用"我测不通"推断服务是死的。**

这条是本次会话用一次真实故障换来的 ——
把 8110 的正常实例当成僵尸停掉，导致公网 502。
