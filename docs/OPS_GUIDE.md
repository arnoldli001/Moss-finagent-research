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

### 2.6 对外实例的自动拉起（值守）

| 项 | 值 |
|---|---|
| 计划任务 | **`MossPilotWatchdog`**（每 5 分钟一次；由用户在 Trae 里创建） |
| 包装脚本 | `scripts/pilot_watchdog.ps1` |
| 实际动作 | `python manage.py ensure --env pilot --port 8110` |
| 死亡现场 | `data/run/backend_incidents.jsonl`（JSONL，含**最后一次活动时刻**） |
| 值守自身异常 | `data/run/pilot-watchdog.log`（**只在退出码非 0 时**写一行） |

```powershell
# 看它有没有在干活 / 最近一次结果
Get-ScheduledTaskInfo -TaskName 'MossPilotWatchdog' |
  Select-Object LastRunTime, LastTaskResult, NextRunTime

# 看历史上"死过几次、死在哪一刻"
Get-Content data\run\backend_incidents.jsonl

# 手动跑一次（健康时完全静默、不写任何日志）
powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\pilot_watchdog.ps1
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

### 8.3 ⚠️ 两处会反复浪费时间的坑（已确认）

1. **全量测试会挂在 WebSocket 用例上**：
   `tests/integration/test_alert_api.py::test_ws_receives_alert_pushed_during_scan`
   里的 `ws.receive_json()` **没有超时**，扫单不推送就永久阻塞
   （实测无任何 CPU 增长）。跑全量请加：
   `--deselect tests/integration/test_alert_api.py::test_ws_receives_alert_pushed_during_scan`
2. **`pytest-randomly` 已装**：不加 `-p no:randomly` 每次顺序都变，
   "挂在第 N 个用例"这类定位会失效。

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
