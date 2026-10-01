# 问题台账 · 在线可用性与工程纪律线（2026-09-30）

> ## 范围与分工（**先读这一段**）
>
> 本轮会话（2026-09-30）由**三个并发主体**推进，各自留下了自己的问题清单。**三份合起来才是全貌**，
> 本文只写其中**一条线**：
>
> | 文档 | 覆盖的线 | 一句话 |
> |---|---|---|
> | `docs/SESSION_PROBLEMS_AND_ROOTCAUSE_20260930.md` | **拥挤度线** | 详情「十几秒才出数据」→ 浮点精度口径 → 库归属 → 自动刷新预算（P1~P9） |
> | `docs/SESSION_PROBLEM_INVENTORY_20260930.md` | **日K线** | 「点击日K卡在正在取日线」→ 无预热作业 → 预热轮不受预算约束（一轮 316.4 s 占住事件循环） |
> | **本文** | **在线可用性与工程纪律线** | 事件循环争用**其余三层**、调度**进程拆分**、启动**就绪**、测量与归因**失范**、测试/门禁**自伤**、出口**护栏** |
>
> **交叉引用（不重复写）**：
> * `daily_warm` 的**硬预算**（316.4 s → 19.8 s、并发 3、预算 90 s / 每只 12 s）是**日K线**的成果，
>   见 `SESSION_PROBLEM_INVENTORY_20260930.md` §1/§3；本文只在「事件循环争用的三层」里引用它的结论。
> * **浮点口径**（一律 3 位有效数字、gzip 36,258 → 22,318 B）是**拥挤度线**的成果，
>   见 `SESSION_PROBLEMS_AND_ROOTCAUSE_20260930.md` P2；本文只收**我在其出口护栏上补的那一刀**
>   （numpy 标量三类漏网，§6.2），并注明归属。
> * 隧道劣化归因（P7）、腾讯云带宽（P9）同样属拥挤度线，本文不重复。
>
> **写法纪律**：每条给 **现象 / 根因 / 修法 / 判据名 / 实测数字 / 诚实边界**；
> 数字一律**现读现算**（核对脚本见 §7）；区分「量到 0」与「没量到」——没量到就写没量到。
> 中文引号一律用「」。
>
> **证据基座**：台账 `docs/REQUIREMENT_CHANGELOG.md`（**150 条**：已闭环 104 / 待办 33 / 待确认 13）·
> `docs/PRD.md`（二十八章）· `tests/`（**6768 条判据 / 355 个文件**）·
> `data/pilot/access_audit/access_audit.jsonl`（**43,702 条**真实请求）·
> `data/pilot/scheduler/runs.jsonl`（作业运行账）· `data/run/*.log`。

---

## 0. 一页速览

| # | 类 | 一句话根因 | **台账**（PRD） | 判据（红了就是回归） | 实测收益 |
|---|---|---|---|---|---|
| 1 | **事件循环争用（三层）** | 长任务与在线 API **共用一条循环**；`async def` 里跑同步重活 | `CHG-0140`（§19.38）· `CHG-0146`（§19.40）· `CHG-0147`（§19.41） | `test_no_loop_blocking_endpoints.py`（2，含自证）· `test_api_no_loop_blocking.py`（7，扩到全部路由） | 端点在飞阻塞 3,056.8→**80.1 ms**；重作业 585 s 期间 pilot **max 14 ms / 0 条 >1 s** |
| 2 | **调度进程拆分** | 拆开一对绑定关系 ⇒ **松开两根绳子**（worker 死了没人知道 / 起两次=双跑） | `CHG-0141`（§19.39） | `test_scheduler_role_split.py`（**16**）· `test_worker_liveness.py`（**14**） | 角色互斥且全覆盖；第二实例被锁拒绝；停止走优雅关停（**checkpoint 主库 1 个**） |
| 3 | **启动就绪** | **端口在监听 ≠ 能应答**（uvicorn 先绑端口后跑 lifespan） | `CHG-0147`（§19.41） | `test_startup_does_not_block_readiness.py`（5） | 启动到就绪 **~20 s → 0.6 s**；重启窗口 **38.2 → 19.3 s** |
| 4 | **测量与归因失范** | 在错的层测量 ⇒ 得到**看起来很确定**的错答案；**受害者常被当成头号犯人** | `CHG-0146`（§19.40）+ `CHG-0140`（§19.38） | `test_inflight_registry.py`（7） | 三轮才收敛；107 条端点复扫 ≥200 ms 的 **2 → 0 条** |
| 5 | **测试/门禁自伤** | 测试会**停掉生产**、缓存会**跨用例污染**、编辑中跑门禁会**假红** | `CHG-0141`（§19.39）· `CHG-0146` ⑭ · `CHG-0148`（—） | `test_test_harness_isolation.py`（3）· `test_cjk_quote_safety.py`（5） | dev 被测试误停 → 已恢复并加**机器闸**；跨文件污染 4 红 → 0 |
| 6 | **出口护栏** | `Mount("/")` 吞掉 WebSocket 作用域；numpy 标量一个根因三种症状 | `CHG-0150`（§19.42）；浮点口径属并发会话 `CHG-0142`（§24） | `test_ws_scope_guard.py`（5）· `test_safe_json_response.py`（6，含本轮新增 1 条类型断言） | WS 非路由路径 **500 → 404**；11 种 numpy 标量全部归一 |

> **台账行号怎么查**（本文件所有 `CHG-xxxx` 都可复跑核对）：
>
> ```bash
> grep -n '| CHG-0146' docs/REQUIREMENT_CHANGELOG.md      # 单行定位
> grep -n '| CHG-01' docs/REQUIREMENT_CHANGELOG.md | tail  # 看最新若干条
> uv run python scripts/prd_sync_check.py --keyword 事件循环  # 三态对账（OK/MISS_LEDGER/MISS_PRD/TODO）
> uv run python scripts/prd_sync_check.py --ledger          # 台账完整性（交付前必须 0 ERROR）
> ```
>
> ⚠️ 台账正文里**带 `\|` 转义**的竖线不要当列分隔符（本文 §7 有统计口径说明）。

---

## 1. 事件循环争用（三层）

> **台账**：`CHG-0140`（≈19.8 s 硬预算，PRD §19.38）· `CHG-0146`（4 个端点移出循环 + 在飞登记，PRD §19.40）· `CHG-0147`（启动/后台任务错峰，PRD §19.41）
> 查法：`grep -n '| CHG-0140' docs/REQUIREMENT_CHANGELOG.md`

### 1.1 现象

用户三小时内**三次**报「前端显示后端不可达」。探针超时线是 **4000 ms**。

### 1.2 根因（三层，逐层量出来的）

**(a) 先纠正我自己的错误归因**（这一条单独记，因为它差点把整件事带偏）：
第一版结论「循环被卡死」的依据是 `backend.log` 里**成片的日志空档**——**那是错的**：
该文件的访问日志行**不带时间戳**，「空档」既可能是「没请求」，也可能只是**排版**。
我把排版读成了停顿。改用带每请求耗时的仪器后：

| 指标（现读现算） | 值 |
|---|---|
| 事故窗口 本地 13:54–13:59（UTC 05:54–05:59） | **112 条**请求，**max 64,393 ms** |
| 其中 13:58 那一分钟 | **23 条**里 **22 条 >1 秒** |
| 全窗口 >4000 ms（探针超时线） | **37 条** |
| 这些请求的最终状态 | **全部 200** ⇒ **是排队，不是报错** |

**(b) 请求路径**：若干 `async def` 处理器里**直接跑同步重活**（FastAPI 语义：`def` 会自动进线程池，
`async def` 则在**事件循环线程**上跑）。

**(c) 计划任务**：四个**纯后台**作业与在线 API 共用同一条循环（累计占用见下）。

**(d) 启动段**：`lifespan` 里 `await` 两段重活（见 §3）。

### 1.3 修法

* **仪器先行**：`src/core/loop_lag.py`（1 Hz 采样、阈值 **500 ms**＝探针超时的 1/8、每 60 s 汇总一行）
  + `src/core/inflight.py`（在飞登记簿：卡顿那一刻读「谁在飞」）。
  ★ 仪器**自己**的测量层坑：**handler 里的耗时恒为 ~0 ms**——被堵的请求**进不到 handler**
  （停在 socket 缓冲区）⇒ 只能量「循环多久没被调度」。
* **请求路径**：四处同步重活改 `await asyncio.to_thread(...)`（`build_cycle` / `scheduler.get_preheat` /
  `_data_status` / `_relevance_stats_sync`）+ `data_watermark`。
* **计划任务**：四个重作业移出在线进程（见 §2）。
* **启动段**：重活挪出「就绪路径」（见 §3）。

### 1.4 判据

* `tests/integration/test_no_loop_blocking_endpoints.py`（**2 条**，常跑 13 条端点 <250 ms，
  含**四个已修端点的回归锁**；`MOSS_LOOPBLOCK_ALL=1` 扫全部无参 GET）。
  ★ 判据带**自证**：同步睡 400 ms 的假 app 必须被量出 ≥300 ms，而 `await asyncio.sleep(400ms)` 必须**不**被判成阻塞。
* `tests/unit/test_api_no_loop_blocking.py`：AST 守卫**扩到全部路由文件**
  （原先只 `parse(routes/quant.py)` + 5 个名字 ⇒ 本轮四个犯人**全在它视野之外**）。
  扩范围**当场抓到第 5 个**（`fundflow.py::search` 的 `keys` 是**字典键**、误报），
  于是补了**必须带理由**的豁免标记 `# loop-blocking-ok: <理由>`。

### 1.5 实测数字（现读现算）

| 端点 | 修前在飞阻塞 | 修后 |
|---|---|---|
| `/api/v1/auction_select/sentiment_cycle` | **3,056.8 ms** | **80.1 ms** |
| `/api/v1/mainline/data/status`（冷路径） | **3,766.7 ms** | **12.4 ms** |
| `/api/v1/mainline/relevance` | 679.1 ms | **15.2 ms** |
| `/api/v1/auction_select/preheat` | 179.7 ms | **12.2 ms** |

* 全量复扫 **107 条**无参 GET：堵循环 ≥200 ms 的端点 **2 条 → 0 条**，最坏 **167 ms**（调试端点）。
* **★ 慢 ≠ 堵**：`quant/data-status` 一次 **10 秒**、`mainline/snapshot` 一次 **14 秒**，
  但分别只堵 **12 / 116 ms**（早已在线程池里）——按 `latency_ms` 排序会把它们长期当成头号问题。
* 四个重作业的累计占用（`runs.jsonl`，**截至本次核对**）：
  `intel_tone_extract` 51 轮 / **25,073 s** / 中位 284.4 s / 最大 **2,036.7 s**；
  `event_alert_intraday` 106 轮 / 13,515 s；`quant_data_sync` 67 轮 / 7,955 s（最大 **585.1 s**）；
  `mainline_daily` 2 轮 / 5,894 s / 中位 ≈ **3,086 s**。**四者合计 ≈ 52,437 s（≈14.6 小时）**。

### 1.6 诚实边界

* 阈值 250 ms 是**经验值**（修后清单内最坏 ≤140 ms），不是推导；
* 未覆盖**带必填参数**的端点与 **POST**；
* 搬进线程池 **≠ 不再排队**（线程打满时慢，但**不再堵事件循环**）；
* 后台任务造成的**毫秒级**空档仍在（§3.6）。

---

## 2. 调度进程拆分：拆分**自带**两类新的静默失效

> **台账**：`CHG-0141`（角色拆分 + worker 存活证据链，PRD §19.39）
> 查法：`grep -n '| CHG-0141' docs/REQUIREMENT_CHANGELOG.md`（⚠️ 该行的 ⑭ 记的是**重启工具的实测坑**——PID 文件与 uv 外壳，**不是** WS 回归；WS 回归的 ⑭ 在 `CHG-0146`）

### 2.1 现象

把四个重作业移出在线进程（`MOSS_SCHEDULER_ROLE` = `api` / `worker` / 未设=全表）之后，
出现两个**原来不存在**的失效面。

### 2.2 根因（拆开一对绑定关系 ⇒ 松开两根绳子）

| 原先**自动成立** | 拆开后 | 若不给答案 |
|---|---|---|
| 进程活着 ⇔ 重作业在跑 | worker **没有端口** ⇒ 它死了前端照旧全绿 | 那 4 个作业**永远不再执行**，且没人发现 |
| 一次只有一个实例 | 起两次 = 重作业**双跑** | 同一分钟往 14 GiB 行情仓**双写**（CHG-0087 的同一形状） |

★ 心跳**必须由独立线程写**：worker 的事件循环**就是**重作业跑的地方
（`mainline_daily` 中位 ≈3,086 s）⇒ 环内写心跳会把**正在干活**的 worker 判成**死的**并重启它——
**把忙判成死，比不监控更糟**。

### 2.3 修法（每条绳子一个答案）

* **心跳**：`worker_heartbeat.py`——写间隔 **20 s**、陈旧阈值 **90 s**（=3×+余量，宁可不报不要误报）、
  **原子替换**（先 `.tmp` 再 `os.replace`）；**三态分开报**（不存在 / 新鲜 / 陈旧），
  外加第四态「内容无法解析 ⇒ 明说读不懂，**不许**说成进程死了」。
* **锁**：`worker_lock.py`——操作系统级（`flock` / `msvcrt.locking`），
  锁属于**打开的文件句柄** ⇒ 进程无论怎么死都由 OS 释放 ⇒ **不存在「陈旧锁」**；
  拿不到锁**必须报出持有者 PID** ⇒ 锁字节**刻意放在内容之外**（`LOCK_OFFSET=4096`，
  因为 Windows 字节范围锁**连读一起拒**）。
* **四条接线**：`ensure`（唯一值守入口，每分钟一轮）后端健康时**也要**确认 worker ·
  `start` **连带**拉起 · `stop`/`--replace` **必须**停掉 worker（否则留**孤儿写者**）·
  新增 `restart-pilot`（**不碰 dev**）。
* **触发路径不止一条**：`_tick()`（每分钟）与 `trigger()`（启动自检补偿）。
  只改前者 ⇒ **后者原样绕过整次拆分**（`_check_quant_sync_at_startup` 正是用 `trigger("quant_data_sync")` 补缺口的）
  ⇒ 新增 `job_out_of_role()` 作**唯一**判据，三处同源。
* **停止通道**：`request_graceful_stop` 走 CTRL_BREAK，而它要求**调用方与目标共享控制台**；
  本项目守护进程全是 `CREATE_NO_WINDOW` 起的、值守跑在计划任务里 ⇒ **实测恒返回 False**
  ⇒ 只能硬杀，而硬杀**不走关停收尾**（不 checkpoint）。改用**停止文件握手**（5 秒轮询），
  不变量是「**旧文件不许杀新进程**」（文件 mtime 必须**晚于**本进程启动时刻）。

### 2.4 判据

`tests/unit/test_scheduler_role_split.py`（**12 条**）：名字必须在注册表里 · `api ∩ worker = ∅`
且 `∪ = 全集` · 未设角色=全表 · 报告把「角色外」与「被裁」分开 · worker 环境与 API **逐项相同** ·
`env_needs_worker()` 必须**派生** · `ensure`/`start`/`stop` 三条接线各一条 ·
★ `trigger()` 在 `role=api` 下**执行器一次都不许被调用**（反面：`role=worker` 下必须照跑）。

`tests/unit/test_worker_liveness.py`（**12 条**）：心跳三态 · 阈值 ≥3× 间隔 · **守护线程**且第一拍立刻落盘 ·
第二个锁拿不到且能说出持有者 · 释放后可再拿 · 锁字节在内容区外 ·
**内容写不进去时锁仍然算拿到**（否则一个旧版进程就能让新版起不来）·
停止文件握手（新文件生效 / 陈旧的必须被忽略并删掉）· 三者同目录。

### 2.5 实测数字

* **端到端**：拆分后第一个真实班次 `quant_data_sync` 于 **16:00:03 在 worker 里跑完**
  （`trigger=schedule`、`status=success`、**585,066 ms**）；
  **同一窗口（本地 16:00–16:12）pilot 服务 55 个请求：max 14 ms、p95 1 ms、>1 秒 0 条**
  （对照事故窗口：max 64,393 ms、22/23 条 >1 秒）。
* **优雅关停**（生产日志逐字）：
  `收到停止文件 …worker.stop（比本进程新）⇒ 开始优雅关停` +
  `worker 关停：释放常驻 SQLite 连接 0 个，checkpoint 主库 1 个`。
* **值守自愈**：worker 由 `MossPilotWatchdog` 那一轮 `ensure` 拉起（事故流水留下 `worker_restart`）。
* **第二实例被拒**：另起一个 worker 时锁判据生效（并因此修掉「旧版锁在偏移 0 导致新版写内容被拒」的崩溃路径）。

### 2.6 诚实边界

* worker **尚未**单独建 Windows 计划任务（目前靠每分钟 `ensure` 兜住，已验证有效）；
* worker 崩溃**无**内存/线程栈现场；worker 上**无** `loop_lag` ⇒「worker 卡住」只能从
  `runs.jsonl` 的 `duration_ms` 间接看出（它卡住时心跳**仍然新鲜**，因为是线程写的）；
* `dev` 实例仍「全跑」（**有意**：擅自改会静默改变本机行为）；
* 手工 `python -m src.scheduler.worker` 不带 `--env` 会跑全表（代码打 ERROR，但**拦不住**）。

---

## 3. 启动就绪与生命周期：**端口在监听 ≠ 能应答**

> **台账**：`CHG-0147`（lifespan 重活交后台 + 分相位计时，PRD §19.41）
> 查法：`grep -n '| CHG-0147' docs/REQUIREMENT_CHANGELOG.md`

### 3.1 现象

重启后一段时间内前端报「后端不可达」，而**进程与端口都是好的**。

### 3.2 根因

* uvicorn **先绑端口、再跑 lifespan** ⇒ 端口已监听而请求排在启动完成之后；
* `lifespan` 里 `await` 着两段重活：指标索引重建（内含对 **1038 个指标**做频率推断，
  日志里就是那 **16 秒**的间隔）+ 数据资产扫描（实测 **210 个资产 / 147,532,584 行 / 16,228.7 MB**）；
* ★ `loop_lag` **永远看不见这一段**——监控是在它之后才启动的
  ⇒「卡顿表里没有它」**≠**「它不卡」。

### 3.3 修法

两段本来就跑在 `asyncio.to_thread`（**不占事件循环**）⇒ 问题不是「阻塞循环」而是「**谁在等它**」
⇒ 改成**具名后台任务**（体内两段 try/except 与原先一字不差），并登记进在飞簿。

**第二半：后台任务必须有名**。就绪变快后仍有 1.2 / 3.6 秒卡顿，而卡顿行印的是
`（无在飞请求）`——对请求路径是对的，但读起来像「没人干活」；真相是**后台任务在干、只是没登记**。
⇒ 新增 `_bg_run(name, coro)`，把 `lifespan` 里**全部 17 个** `create_task` 换成它。
实测点名：`task:catalog-rebuild(102281ms)`、`task:fundflow-warm(...)`、`task:quant-sync-startup-check(...)`。

### 3.4 判据

`tests/unit/test_startup_does_not_block_readiness.py`（**5 条**）：
AST 断言 `lifespan` 里不许 `await` `rebuild_all`/`scan_and_store`（**按语法树判、不按文本**——
那段注释**逐字写着**这两个名字，文本判据会被自己的说明命中而假红）·
AST 断言必须交给具名后台任务且它存在 · **行为**判据（把 `rebuild_all` 换成「睡 2.5 秒」的替身
⇒ 首个请求必须 2 秒内返回**且替身确实被执行**）· `lifespan` 里不许出现裸 `asyncio.create_task` ·
**行为**：真跑一次 `_bg_run`，断言运行期间登记簿看得到、结束后注销。

### 3.5 实测数字

* **启动到就绪**：`运行环境：pilot` 17:24:23.320 → `Application startup complete` 17:24:23.933
  ⇒ **≈0.6 秒**（整改前两段重活合计 ≈20 秒排在应答之前）；
* **重启总窗口**：`38.2 s → 19.3 s`（分相位：停止段 **5.7~6.1 s** + 端口释放 **0.4 s** +
  启动命令 **11.1~11.9 s**）；
* 后台两段照常完成（`指标索引已重建：元数据 1246 条，回填 2233 个指标`、
  `数据资产扫描完成：210 个资产…合计 147,532,584 行`），且**都出现在就绪之后**。

### 3.6 诚实边界

* 「错峰」只做了**最贵的两段**；其余 13 个启动/后台任务实测合计只有 ms 级空档
  （进程内 harness 实测启动段最大空档 **45 ms**），**没有**逐个错峰——它们不是当前瓶颈；
* 后台重建与首个请求仍会**抢 CPU/磁盘**（它只是不挡就绪，不是不耗资源）；
* 判据的 2 秒阈值比真实启动（0.6 s）宽 3 倍，留的是测试机抖动余量，**不是**「允许 2 秒」；
* 重启窗口剩下 19 秒里，启动命令 11.2 s 的大头是**多次 PowerShell 进程枚举**
  （每次 ~1.1 s）——再压需换掉命令行枚举（改用 PID 文件 + 启动时间戳），本轮**没做**。

---

## 4. 测量与归因失范：**受害者常被当成头号犯人**

> **台账**：`CHG-0146`（在飞登记把「谁在堵」变成可复现事实，PRD §19.40）· `CHG-0140`（`loop_lag` 指标与硬预算，PRD §19.38）
> 查法：`grep -n '| CHG-0146' docs/REQUIREMENT_CHANGELOG.md`

### 4.1 现象

「哪个端点在堵事件循环」这个问题，**前两轮的答案都是错的**。

### 4.2 根因（三轮收敛记录，方法论核心）

| 轮 | 仪器 | 得到的「头号犯人」 | 为什么错 |
|---|---|---|---|
| ① | `access_audit.latency_ms` 排序 | `/api/v1/research/{task_id}`（p50 **8.3 s**、184 次） | 它只是**内存字典查表**；慢是因为研究期间前端**轮询它最频繁** ⇒ 总在队里，把**别人的阻塞**记在自己账上（**受害者**） |
| ② | `loop_lag` + 在飞点名 | `fundflow/snapshot` | 只是**共现**：聚焦重放 6 次全是 120~210 ms、**不复现** ⇒ 那次是**冷路径** |
| ③ | **进程内 harness**（同一条循环 + 20 ms 心跳采样） | 见 §1.5 四行 | ✅ 确定性、可复现、可直接当判据 |

**纪律**：`latency_ms` 混着**排队**时间 ⇒ 按它排序会**系统性地把头号受害者排第一**；
区分「谁堵的」与「谁在排队」必须在**卡顿那一刻**看谁在飞；**定罪必须可复现重放**。

### 4.3 修法（两件仪器 + 一处判据修正）

* `src/core/inflight.py`：卡顿行变成
  `…卡顿时刻在飞：GET /api/v1/fundflow/snapshot(2641ms)`；
  而**没有**请求在飞时输出 `（无在飞请求）`——**那同样是结论**（实测 16:29~16:36 的卡顿全部如此
  ⇒ 来自启动/后台预热）。
* `tests/integration/test_no_loop_blocking_endpoints.py`：进程内 harness（`gap_max_ms` = 该请求存活期间
  循环最长一次没被调度的时间）。
* ★ **仪器第一版完全没生效，而形状判据照样是绿的**：登记依赖挂在
  `api_router.dependencies.append(...)` 上——而 FastAPI 的 `include_router` **只应用调用时传进来的**
  `dependencies=`，事后 append **不生效**。⇒ 改到**唯一挂载点**
  `app.include_router(api_router, dependencies=[Depends(_mark_inflight)])`，
  并把判据改成**行为**判据（真发一个请求、断言登记被调用）。
  **判「接线」要看电流，不要看电线在不在。**

### 4.4 判据

`tests/unit/test_inflight_registry.py`（**7 条**）：登记/注销成对 · 按持续时间降序 ·
无在飞时 `describe()` 为空串 · 无事件循环时静默跳过 · **行为**判据（真请求必须触发登记）·
卡顿理由里**必须点名**在飞的处理器 · ★ **非 HTTP 作用域也成立**（见 §6.1）。

### 4.5 诚实边界

* 在飞点名的名单是「**谁在飞**」不是「谁在算」——长期 `await` 的任务（如每 25 秒一次心跳的
  `alert-hub-keepalive`）也会一直挂在名单里 ⇒ 它是**嫌疑人名单，不是判决书**；
* `p50` 在「循环健康」时才近似处理器自身成本；长阻塞窗口里它会被系统性抬高；
* 全量扫描（`MOSS_LOOPBLOCK_ALL=1`）要几分钟，**不是**每次门禁都跑。

---

## 5. 测试与门禁自伤（AI 协作 + 并发工具链）

> **台账**：`CHG-0141`（`_forbid_real_process_signals` 只对活进程生效 + `_reset_settings_cache` 自动清理，PRD §19.39）· `CHG-0146` ⑭（WS 回归：全局依赖用 `Request` 打崩 WS 作用域）· `CHG-0148`（门禁读数与 WS 边角首次登记）
> 查法：`grep -n '| CHG-0148' docs/REQUIREMENT_CHANGELOG.md`

> 这一类的共同点：**都不是业务逻辑错，而是「我和另一个主体同时动手」或「工具在错的时机动手」
> 产生的时序型缺陷**。四条里有两条是**我自己造成的生产事故**，如实记。

### 5.1 测试**停掉了生产**（本轮最贵的一次自伤）

* **现象**：全量门禁后 `dev`(8100) 实例消失，事故流水连续三次 `restart` + `restart_ok`。
* **根因**：`stop_backend_processes` 为支持 worker 加上了 `list_our_worker_pids()` 之后，
  四条既有用例**只 monkeypatch 了 `list_our_backend_pids`**、**没 patch `request_graceful_stop`**
  ⇒ 它们枚举到的是**真实 PID**，于是对真实进程发了 CTRL_BREAK。
* **修法（两道）**：① 四条用例补齐 patch + 新增正向判据（worker 必须收到优雅请求、
  赖着不走必须被清掉、结果必须回报）；② `tests/conftest.py` 新增 **autouse 安全闸**：
  `kill_pid_tree` / `request_graceful_stop` **只要目标 PID 真的活着就拒绝执行**。
  ★ **只拦「活着的 PID」是关键**：故意验证真实实现的用例（「对不存在的 PID 必须返回 False」）
  传的是假 PID，应继续走真实分支——一律拦住会把判据变成「测一个替身」。
* **判据**：`tests/unit/test_test_harness_isolation.py`（3 条，其中一条**直接调夹具本体**验证它真的清）。

### 5.2 跨用例**缓存污染**（单跑全绿、合跑全红）

* **现象**：四条 `/health` 契约判据返回 **401**（被当成公网实例），而它们**单独跑都过**。
* **根因**：我的夹具设了 `MOSS_ENV=pilot`，而 `heartbeat_path()` 在缺 `SCHEDULER_DIR` 时回落
  `get_settings()` ⇒ **一份 pilot 的 `Settings` 被 `lru_cache` 缓存进进程**。
* **修法**：① 夹具同时重定向 `SCHEDULER_DIR`；② conftest 新增 autouse `_reset_settings_cache`
  （每个用例前后 `get_settings.cache_clear()`）——把「记得清缓存」升级为**全局不变量**。
* **复现命令**：`pytest tests/unit/test_scheduler_role_split.py tests/unit/test_warehouse_write_ownership.py -q`
  （修前 4 failed / 修后 38 passed）。

### 5.3 **编辑中跑门禁 = 假红**

* **现象**：5 条 `test_tunnel_watchdog` 红，而它们与我的改动无关。
* **根因**：我在 20 分钟全量门禁**跑动期间**改了 `manage.py`；这些判据用 `inspect.getsource`
  **按行号**取源码 ⇒ 取到了别的函数。
* **修法/纪律**：门禁跑动期间**冻结**被扫文件；或识别该症状（getsource 返回兄弟函数）后复跑。
  文件稳定后复跑 **108 passed**。

### 5.4 并发协作者：三种撞车与处置

| 撞车 | 现场 | 处置 |
|---|---|---|
| **共享台账编号** | 我两次取的号（0142/0144/0149）被并发会话先占 | 插入**前一刻**现读最大号；长中文文案**外置 `.md` 载荷**（见 §5.5） |
| **他人半保存状态** | 21:00 出现 `NameError: threading`（并发会话保存 `warm.py` 的中间态），21:01 落盘后 import 自检通过 | **先复现 + 看 mtime** 再判归属；不在他人「在飞」文件上动手（**陈旧缺陷则照修**，并登记归属） |
| **他人在搬数据库** | 25 条跨模块红灯（`sector_crowding` 6 / `platform_data_connector` 6 / `safe_json` 1 …），**单独跑都过** | 分类顺序：**先单跑复现**，再判归属；**不要先怀疑自己的改动** |

### 5.5 我自己的一类高频自伤：**把长中文写成 Python 字符串**（11 次）

* **现象**：中文行文里混进 ASCII 引号 / 漏闭合 ⇒ **文件整体语法错误**，
  而报错信息（`invalid character`、`Perhaps you forgot a comma?`）**从不指向真因**。
* **修法（三层，缺一不可）**：① 判据 `tests/unit/test_cjk_quote_safety.py`（5 条）——
  **扫 `src` + `tests` + `scripts` 的每个 `.py` 必须能被 AST 解析**（零误报；带 BOM 文件按 `utf-8-sig` 读）；
  ② 临时脚本一律放 `scripts/`（**放 `data/` 就落到判据覆盖范围之外**——这条教训我付了第 7~11 次）；
  ③ **长中文文案外置 `.md` 载荷 + 脚本读取** ⇒ 引号混用那一类从此在**载荷层**不可能发生。

### 5.6 诚实边界

* 安全闸只覆盖 `manage.py` 的两个发信号函数；其它「测试碰真实世界」的入口（网络、真实 DB 写入）
  仍是**逐用例纪律**，没有全局闸；
* `_reset_settings_cache` 只清 `get_settings`；其它带 `lru_cache` 的单例（如 `load_config`）
  仍靠各文件自觉（已知有文件因此踩过坑）；
* 「编辑中跑门禁」目前是**纪律**，没有机器判据。

---

## 6. 出口护栏：作用域与数据契约

> **台账**：`CHG-0150`（WS 作用域 shim + numpy 标量出口归一，PRD §19.42）；**浮点口径那条属并发会话的 `CHG-0142`**（PRD §24；同一台账行还登记了 §25 隧道带宽，与本线无关），不在本线
> 查法：`grep -n '| CHG-0150' docs/REQUIREMENT_CHANGELOG.md`

### 6.1 WS 作用域边角：**500 → 404 / 1008**

* **现象**：给非 WebSocket 路径发带升级头的请求（或连一个**不存在**的 WS 路径）⇒ 客户端 **500**。
* **根因**：SPA 静态资源挂在 `/`（`Mount("/")` 匹配**任何**路径），而
  `starlette/staticfiles.py:91` 第一行是 `assert scope["type"] == "http"` ⇒ 任何落进去的
  WebSocket 作用域都变 `AssertionError` ⇒ uvicorn 记 `connection rejected (500 Internal Server Error)`。
* **修法**：5 行 ASGI shim `_HttpOnlyStatic`（不改第三方、不注册兜底路由）——HTTP 原样透传；
  WS 支持「拒绝响应」扩展时回 **404**（可读、可排障），否则按规范在 accept 之前 `websocket.close(1008)`；
  lifespan 等其它作用域不参与。
* **公网实测**（主用 `hk.wujiaitool.cn`）：
  `/definitely-not-a-ws` 与 `/api/v1/health/live` + 升级头 ⇒ **404**（修复前 500）；
  `/api/v1/ws/intraday`、`/api/v1/ws/alerts` ⇒ **403 不变**（存在、但未登录）；
  `/api/v1/health/live`、`/healthz`、`/` ⇒ 200（静态挂载未被改坏）。
  ★ **404 与 403 必须能区分**：前者「根本没有 WS 路由」，后者「有、但没登录」；
  混为一谈的话「WS 路由被误删」会伪装成「反正都是拒绝」。
* **判据**：`tests/unit/test_ws_scope_guard.py`（**5 条**）：三种「没有 WS 路由」的路径必须 404/1008
  且**不能**是 500 · 静态挂载仍服务 HTTP · **shim 合同**（直接对挂载对象断言：
  HTTP 透传、WS 不进内层、扩展缺失时 `close(1008)`、lifespan 不参与）。
  ★ 这条第一版写成「通过整站连 WS 判断」，而测试环境没装配 runtime ⇒ 处理器自己抛错，
  **判据在测别的东西**——已改成 shim 级。

### 6.2 numpy 标量：**一个根因、三种症状**（归属：收口并发会话那条红灯）

> **归属说明**：浮点口径（3 位有效数字）是**拥挤度线**的成果，见
> `SESSION_PROBLEMS_AND_ROOTCAUSE_20260930.md` P2。本节只记**我在其出口护栏上补的那一刀**。

| 输入 | 修复前 | 用户看到 |
|---|---|---|
| `np.float32('nan')` / `np.float16('nan')` / `np.float32('inf')` | 原样漏到 `json.dumps` | **500**（`ValueError: Out of range float values`） |
| `np.int64(7)` / `np.int32` / `np.uint8` | 被压成 `7.0` | **类型漂移**（成交量/条数/根数变浮点） |
| `np.bool_(True)` | 既非 `bool` 也非 `numbers.Real` | **500**（`TypeError: not JSON serializable`） |

* **根因（一个）**：`np.float32` **不是** `float` 子类（`np.float64` 才是）
  ⇒ 走不到 `isinstance(value, float)` 分支，而兜底的 `Real` 分支**没有非有限判断**
  （`round_significant` 的契约是「非有限原样返回」）。
* **修法**：`Real` 分支补 `math.isfinite`；`Integral` 保持 `int`；
  新增 `.item()` 归一分支（numpy 标量转原生标量后**重走一遍本函数**）——
  三条规则**只写一遍**（抄第二遍就是给下次漂移留位）。
* **实测**：**11 种** numpy 标量逐个过（nan/inf 各族 → `None`；`np.float32(2.5)` → `2.5`；
  `np.int64/32/uint8` → **int**；`np.bool_` → **bool**）。
* **判据**：`test_safe_json_response.py` 新增 1 条，**显式断言类型**——
  因为 `7 == 7.0` 与 `True == 1` 都为真，只查值会放过类型漂移。

### 6.3 全局依赖会打挂**非 HTTP 作用域**（本轮全量门禁抓到的真回归）

* **现象**：全量门禁 **27 failed + 7 errors**，其中 **2 个 WS 判据 + 7 个 health 契约判据**一起红。
* **根因**：登记依赖挂在**汇总 router** 上 ⇒ 它同时作用于 WS 路由，而 WS 的 scope 里**没有** `request`
  ⇒ `TypeError: _mark_inflight() missing 1 required positional argument`。
  **形状很值得记**：它**不是**「WS 少了个登记」，而是「**给所有路由加依赖时没问这些路由里有没有非 HTTP 的**」。
* **修法**：依赖改收 `HTTPConnection`（`Request` 与 `WebSocket` 的**共同基类**）。
* **判据**：§4.4 的第 7 条（用**独立小应用**挂同一依赖 + 一条真 WS 路由，断言「连得上、收得到、结束后注销」）。
  ★ 写这条判据时又踩到一个隐蔽坑：本文件有 `from __future__ import annotations`
  ⇒ 注解是字符串、FastAPI 用 `func.__globals__` 解析；把 `WebSocket` 等 import 写在**测试函数内部**
  ⇒ 解析失败 ⇒ FastAPI 把 `websocket` 当成**查询参数**（症状是 `WebSocketDisconnect(1008)` 且
  reason 里写着 `loc=['query','websocket'] Field required`，**与「依赖挂错」长得完全不一样**）。
  修法：这些 import 必须放**模块顶层**。

### 6.4 诚实边界

* WS 拒绝路径**只在主用入口实测**（本机 + `hk.wujiaitool.cn`）；备用 CF 入口当前首页 403（已登记形态）；
* WS 的**真实客户端**行为（浏览器控制台表现）未逐一验证，只验到握手层 404/403；
* 出口护栏的豁免清单（时间类字段原样带走）由 `float_precision.EXEMPT_FIELDS` 维护，
  新增字段漏登记时表现为「该字段没被压精度」——**没有**自动发现机制。

---

## 7. 数字核对方式（可复跑）

本文所有数字都在 2026-09-30 当晚**现读现算**，核对脚本 `data/_verify_numbers.py`（临时件，已删）：

| 数字 | 来源 | 核对方式 |
|---|---|---|
| 事故窗口 112 条 / max 64,393 ms / 13:58 22-of-23 / 37 条 >4 s | `data/pilot/access_audit/access_audit.jsonl` | 按 UTC 05:54–05:59 过滤（**本地 = UTC+8**） |
| `quant_data_sync` 585,066 ms @16:00:03 | `data/pilot/scheduler/runs.jsonl` | `job_name in HEAVY and start_time startswith 16:0` |
| 四个重作业累计 51/106/67/2 轮、合计 ≈52,437 s | 同上 | 按 `job_name` 分组求和（**含本轮之后新增的班次**，故与 PRD §19.38 的旧口径不同） |
| pilot 55 条 / max 14 ms / 0 条 >1 s | `access_audit.jsonl` UTC 08:00–08:12 | 同 ② |
| 启动到就绪 ≈0.6 s | `data/run/backend.log` | `运行环境：pilot` 与 `Application startup complete` 的时间戳 |
| 107 条端点 / ≥200 ms 0 条 / 最坏 167 ms | `data/run/_loopblock_harness.json` | 读 JSON 排序 |
| 台账 150 条（104/33/13）· 类型分布 | `docs/REQUIREMENT_CHANGELOG.md` | 按**未转义的** `\|` 切列统计 |
| 6768 条判据 / 355 文件 / src 455 文件 8.35 MB | `tests/`、`src/` | `pytest --collect-only`、文件计数 |
| 各判据文件的「条数」 | 本文 §0 表 | 口径 = **`pytest --collect-only` 收集到的用例数**（参数化展开后才算），**不等于** `def test_` 函数个数（例：`test_ws_scope_guard.py` 2 个函数 → 5 条；`test_scheduler_role_split.py` 14 个函数 → 16 条；`test_inflight_registry.py` 4 个函数 → 7 条） |

### 7.1 本文 ↔ 台账 ↔ PRD 的对照与查法

| 本文 | 台账条目 | PRD 锚点 | 台账行号（2026-09-30 当晚） |
|---|---|---|---|
| §1（三层争用） | `CHG-0140` / `CHG-0146` / `CHG-0147` | §19.38 / §19.40 / §19.41 | 181 / 414 / 415 |
| §2（进程拆分） | `CHG-0141` | §19.39 | 408 |
| §3（启动就绪） | `CHG-0147` | §19.41 | 415 |
| §4（测量归因） | `CHG-0146` + `CHG-0140` | §19.40 / §19.38 | 414 / 181 |
| §5（门禁自伤） | `CHG-0141` · `CHG-0146` ⑭ · `CHG-0148` | §19.39 / §19.40 / — | 408 / 414 / 416 |
| §6（出口护栏） | `CHG-0150`（§19.42）· `CHG-0142`（浮点口径属并发会话；该行台账还含 §25 隧道带宽） | §19.42 / §24 / §25 | 417 |

**怎么按 `CHG-xxxx` 回查原文**（行号会随后续追加而漂移，**以 `grep` 结果为准**，上表只是当晚快照）：

```bash
# ① 单条定位：拿到行号后直接读那一行
grep -n '| CHG-0146' docs/REQUIREMENT_CHANGELOG.md

# ② 看最新若干条（确认「下一个可用 id」，避免与并发会话撞号）
grep -n '| CHG-01' docs/REQUIREMENT_CHANGELOG.md | tail -5

# ③ 关键词三态对账：OK / MISS_LEDGER / MISS_PRD / TODO
uv run python scripts/prd_sync_check.py --keyword "事件循环"

# ④ 交付前必跑：台账完整性，必须 0 ERROR
uv run python scripts/prd_sync_check.py --ledger
```

⚠️ 两个**已实测**的坑：

* 台账正文里存在 `\|`（**转义竖线**，多写在证据/数字里）。按裸 `|` 切列会把状态列切错 ⇒
  用 `(?<!\\)\|` 切；本文的「150 条 = 104/33/13」就是按这个口径算的。
* 台账 id 会被**并发会话同时追加**（本线期间撞号 3 次：`0142`/`0144`/`0149`）⇒ 写入前先做 ②，
  不要把「我读到的最大 id + 1」当成保证。

---

## 8. 未闭环（本线相关，诚实登记）

| 项 | 状态 | 说明 |
|---|---|---|
| worker 的 Windows 计划任务 | **未做** | 目前靠每分钟 `ensure` 兜住（已验证有效）；`pilot_autostart.ps1` 未改 |
| worker 崩溃现场（内存/线程栈） | **未做** | 只有 `worker_restart` 事件流水；对比后端那次的 `memory_snapshot()` 是缺口 |
| worker 侧 `loop_lag` | **未做** | 它卡住时心跳**仍新鲜**（线程写），只能从 `duration_ms` 间接看出 |
| B 类后台任务进一步错峰 | **未做** | 仪器已证明存在（`（无在飞请求）`），但实测只有 ms 级空档，**不是当前瓶颈** |
| 重启窗口剩余的 19 秒 | **未压** | 其中启动命令 ~11 s 的大头是多次 PowerShell 进程枚举（~1.1 s/次） |
| 「编辑中跑门禁」 | **仅纪律** | 无机器判据；症状是 `inspect.getsource` 返回兄弟函数 |
| 测试台架安全闸的覆盖面 | **部分** | 只覆盖 `manage.py` 的两个发信号函数；网络/真实 DB 写入仍靠逐用例自觉 |
| `CHG-0148` 余项 | **待办** | WS 边角已修（本文 §6.1）；其余是「体检基线定期复跑」 |
| 三条存量红灯（`CHG-0121`） | **待办** | 属他人特性，**不许代改**，已显式登记 + 链接待办 |
| `docs/` 与部分 `configs/*.yaml` 的入库政策 | **待裁定** | 「是否随仓库发布」是政策判断，不该由单方决定 |

---

*本文只覆盖**在线可用性与工程纪律**这条线；另两条线见
`docs/SESSION_PROBLEMS_AND_ROOTCAUSE_20260930.md`（拥挤度）与
`docs/SESSION_PROBLEM_INVENTORY_20260930.md`（日K）。三份合起来才是本轮全貌。*

---

## 附：同主题文档地图（避免"两套互相竞争的事实源"）

本轮**同一主题**（事件循环争用 / 进程拆分 / 启动就绪 / 出口路径）在仓库里出现过**两套并行记录**（两个 AI 会话几乎同时产出）。为避免读者拿两份互相矛盾的清单，这里给出地图：

| 文件 | 行数 | 本份的关系 |
|---|---|---|
| **本文件** | 523 | ✅ **建议作为主记录**：六类分组 + 每类含判据名 + 台账 `CHG` 逐条锚定 + 数字核对方式 + 未闭环登记 |
| `docs/PROBLEM_INVENTORY_EVENTLOOP_AND_SPLIT_20260930.md` | 339 | 并发协作者的同主题清单（写法更短、按交付批次组织）；**两者冲突时以本文件为准**，但它的补充视角值得对照 |
| `docs/SESSION_PROBLEMS_AND_ROOTCAUSE_20260930.md` | 470 | **另一条线**（拥挤度详情慢 → 浮点精度 → 库归属 → 自动刷新），与本份互补 |
| `docs/SESSION_PROBLEM_INVENTORY_20260930.md` | 92 | 又一条线（日K 预热），与本份互补 |
| `docs/AI_DEV_ANTIPATTERN_INDEX.md` | 162 | 交付②（AI 反模式）的**索引**；纪律正文在 `.trae/skills/ai-defect-and-misread-guard` §6/§8/§9 与 `ai-session-hygiene-and-concurrency-guard` |
| `docs/INTERVIEW_HIGHLIGHTS_EVENTLOOP_OPS_20260930.md` | 632 | 交付③（面试材料，本主题） |

⚠️ **`.trae/skills/*` 与 `docs/INTERVIEW_*.md` 受 `.gitignore` 管辖**（`:160`、`:164`）：除 `requirement-prd-sync` 外**默认不入库**（面试稿含个人域名与策略明细，是有意不发布）。所以 `AGENTS.md` 里的 `@.trae/skills/...` 引用**只在本地工作副本里可解析** —— 这是既定政策，不是缺陷；若要随仓库发布，需 `git add -f` 并在 `.gitignore` 放行对应路径。
