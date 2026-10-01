# 本轮问题清单 · 根因 · 解决方案（2026-09-30 事件循环 / 进程拆分 / 启动就绪 / 出口路径）

> ⚠️ **归属裁决（2026-09-30，用户定）**：本主题的**主记录**是
> `docs/PROBLEM_LEDGER_EVENTLOOP_AND_OPS_20260930.md`（六类分组 · 判据名 · CHG↔PRD 锚点 · 数字核对方式 · 未闭环登记）。
> **本文件降为参考视角**：它按"18 条 × 交付批次"组织，视角与主记录互补，可对照阅读；
> **两者若有出入，以主记录为准**。本次只加这一段指针，**正文一字未动**。

> **口径与边界**：本文件只收**本会话**（2026-09-30 下午至夜间）新增或本届首次定位的问题共 **18 条**，
> 每条都给：**现象 → 根因（机制）→ 修法 → 机器判据 → 实测证据 → 残留/边界**。
> 全项目历史清单**不在这里重复**，见 §6 的索引表。
>
> ★ 本文件的证据全部是**实测读数**（命令输出 / JSONL 流水 / 计数器），不是推论；
> 「没量到」的地方显式写「未量到」（本项目硬约束：**"没量到" ≠ "量到 0"**）。

---

## 0. 一句话总览

用户这一轮的核心诉求是「**前端不可达出现频次太高，彻底解决**」，随后是 P0 的三件：
① 把 4 个重作业挪出在线进程 → ② 把阻塞端点移出事件循环 → ③ 启动/后台任务不再挡就绪。
三条做完后，**可用性问题的两个来源各被治掉一半**，并把治理过程**仪器化**了
（下一次同类问题**能被指名**，而不是再靠猜）。

| 类别 | 条数 | 一句话 |
|---|---|---|
| A 事件循环与可用性 | 6 | 先有仪器（能指名），再按占用治大头 |
| B 进程与运维 | 5 | 拆分会松开"活着⇔在干活"与"一次只有一个"两根绳子 |
| C 出口与协议 | 2 | 出口护栏漏一类输入 ⇒ 用户看到 500 |
| D 我的协作缺陷（自伤） | 5 | 全部已机器化拦截（有判据名） |

---

## A. 事件循环与可用性（6 条）

### A1 · 前端三次报「后端不可达」，而请求全部最终 200

* **现象**：13:54–13:59 请求最慢 **64,393 ms**；13:58 那一分钟 **23 条里 22 条 >1 秒**；
  前端探针超时是 **4,000 ms** ⇒ 必然显示「不可达」。**全部请求最终 200**（是排队，不是报错）。
* **根因（机制）**：**单进程 uvicorn 只有一个事件循环**；所有作业与在线 API 共用它。
  只要有一个同步重活占住循环，同一刻所有人的请求一起排队。
* **修法**：**先装仪器**（`src/core/loop_lag.py`：1 Hz 采样、阈值 500 ms = 探针超时的 1/8、
  每 60 s 一行、`note_probe()` 计存活探针次数以区分"循环卡了"与"根本没请求"），
  再按**累计占用**排序决定治谁。
* **判据**：`tests/unit/test_metrics.py`（异常种类 ↔ 界面 label 必须一一对应：
  `kind_labels` 覆盖 `job_budget` / `loop_lag`）。
* **证据**：首个汇总行 `采样 58 次 max=1047ms 超阈值 2 次 | 存活探针 16 次 max=0ms`。
* **残留**：仪器只在 **pilot 进程**里跑；worker 进程没有它（它不服务 HTTP）。

### A2 · 我的第一次归因是错的：把「日志排版」读成了「循环停顿」

* **现象**：我据 `data/run/backend.log` 的**成片空档**断定"事件循环卡死"。
* **根因**：该文件的**访问日志行不带时间戳**（时间戳只在前面的 uvicorn 摘要里）⇒
  空档既可能是"没请求"、也可能只是**排版**。
* **修法**：改用**带每请求耗时的仪器**（`data/pilot/access_audit/access_audit.jsonl`，
  每条含 `at` / `latency_ms` / `status`）。
* **纪律（已写进 PRD §19.38）**：判"事件循环停没停"必须用**每请求耗时**这一面，不许用日志行距。
* **残留**：`access_audit` **不记公开路径**（`/health/live` 被跳过）⇒
  "决定横幅的那个探针"在审计里一条都没有，这正是后来补 `note_probe()` 的原因。

### A3 · `daily_warm`（日K预热）把循环按住 316 秒

* **现象**：单轮墙钟 **316.4 s**、22 只失败；最坏情形 80 只 ÷ 并发 3 × 每只 30 s ≈ **810 s**
  —— 是它自己预算的 **9 倍**。
* **根因**：软超时（每只一个 timeout）**没有任何整轮上限**，最坏路径没有界。
* **修法**：`asyncio.wait_for(gather(...), timeout=max(0, 预算 − 已用))` **硬截**；
  超时取消未完成项并**重算三态**（`warmed=True` / `failed=False` / 取消的记 `skipped`
  —— **"没跑完"不许冒充"失败"**）。常数：并发 3 / 最多 80 只 / 预算 90 s / 每只 12 s。
* **判据**：`tests/unit/test_daily_warm.py::test_round_wall_clock_is_hard_capped_by_budget`
  （20 只全挂死 + 每只 5 s + 并发 1 + 预算 0.5 s ⇒ **必须 3 秒内返回**）、
  `::test_budget_hit_records_an_anomaly`、`::test_warm_concurrency_is_justified_by_measurement`
  （**改并发值之前必须先重测那张表** —— 我把它调到 5 时被这条当场打回）。
* **证据**：改造后同一条流水 `duration_ms=19838`（316.4 s → **19.8 s**）、失败 22 → 3。
* **残留**：会**主动放弃**尾部 code（记 `skipped`）——有意取舍：宁可少预热几只，也不再让首屏等 5 分钟。

### A4 · 4 个纯后台重作业占了事件循环的**大头**（累计 ≈5 万秒）

* **现象**：`intel_tone_extract` 24,849 s/49 轮（中位 **304 s**、最大 **2,037 s**）、
  `event_alert_intraday` 13,201 s/104、`quant_data_sync` 5,943 s/59、
  `mainline_daily` 5,894 s/2（中位 **2,947 s**）。
* **根因**：它们对实时性**零要求**（产物落库/落盘），却与在线请求**共用同一个循环**。
* **修法**：**进程角色拆分**（见 B1）——API 进程 `role=api` 不跑它们，worker 进程 `role=worker` 只跑它们。
* **证据（生产，最强的一条）**：拆分后第一个真实班次 `quant_data_sync` 于 **16:00:03 在 worker 里跑完**
  （`trigger=schedule`、success、**585,066 ms**）；同一窗口 pilot 的在线服务
  **55 条请求、max 14 ms、p95 1 ms、>1 秒 0 条**（对照：拆分前同类窗口 max **64,393 ms**、22 条 >1 秒）。
  同窗口循环延迟逐分钟：`906(1 次超阈)/453/94/640(1)/157/375/32/16/16/16/16 ms`。
* **判据**：`tests/unit/test_scheduler_role_split.py`（两角色**互斥且合起来是全集**）+
  `tests/integration/test_no_loop_blocking_endpoints.py`。
* **残留**：该窗口仍有 **2 个单次采样**越过 500 ms 阈值（量级 640–906 ms）——不是"零卡顿"。

### A5 · 请求路径上还有端点在 `async def` 里做同步重活

* **现象（定罪读数，进程内 harness）**：`auction_select/sentiment_cycle` 总 3,110 ms 里
  **堵循环 3,056.8 ms**；`mainline/data/status`（冷）**3,766.7 ms**；`mainline/relevance` 679.1 ms；
  `auction_select/preheat` 179.7 ms。
* **根因**：FastAPI 语义 —— `def`（同步处理器）会被自动丢线程池；而 **`async def` 里的同步调用
  直接跑在事件循环上**。这四个端点都是后者（`build_cycle` / `_data_status` / 同步 SQLite / `get_preheat`）。
* **修法**：整段搬进 `await asyncio.to_thread(...)`（仓库既有范式）；
  同步主体抽成 `_relevance_stats_sync()` 便于入池。
* **证据（同一条判据复测）**：3,056.8→**80.1** · 3,766.7→**12.4** · 679.1→**15.2** · 179.7→**12.2** ms；
  107 条无参 GET 中"堵循环 ≥200 ms"的端点 **2 条 → 0 条**（最坏剩 167 ms，一个调试端点）。
  ★ 反直觉：`quant/data-status` 一次 **10 秒**、`mainline/snapshot` 一次 **14 秒**，
  但它们只堵 **12 / 116 ms**（早已在池里）—— **"慢"与"堵"是两件事**。
* **判据**：`tests/integration/test_no_loop_blocking_endpoints.py`（2 条，8.8 秒，含**自证**：
  同步睡 400 ms 必须被抓到 ≥300 ms，而 `await sleep(400ms)` **不许**被判成阻塞）+
  `tests/unit/test_api_no_loop_blocking.py`（AST 守卫**范围扩到全部路由文件**，含带理由的豁免标记
  `# loop-blocking-ok`）。
* **残留**：未覆盖**带必填参数**的端点与 POST。

### A6 · 启动段被重活挡住：**端口在监听、请求却排在 20 秒之后**

* **现象**：日志时间线 17:03:12.4（catalog 作业已注册）→ **17:03:28.5**（频率推断修正 1,038 个指标）
  = 中间 **16 秒** → 17:03:29.3 资产扫描。uvicorn **先绑端口、再跑 lifespan** ⇒
  重启期间前端 4 秒探针必然超时，**用户看到「后端不可达」，与"进程真的死了"长得一模一样**。
* **根因（关键）**：这两段**本来就跑在 `asyncio.to_thread`（不占循环）** ——
  问题不是"阻塞循环"，而是"**谁在等它**"（lifespan 里 `await`）。
* **修法**：改成具名后台任务 `_rebuild_catalog_and_assets_later`（体内 try/except 与原先一字不差），
  并登记进**在飞簿**；顺带把 lifespan 里**全部 17 个** `create_task` 收进 `_bg_run(name, coro)`
  （起任务 + 登记 `task:<名字>`）。
* **证据**：启动到就绪（`Application startup complete`）**~20 秒 → 0.6 秒**；
  重启总窗口 **38.2 → 19.3 s**；后台两段照常完成（`指标索引已重建：元数据 1246 条 / 回填 2233 个指标`、
  `数据资产已扫描：210 个资产 / 147,532,584 行 / 16,228.7 MB`），且**都在就绪之后**。
* **判据**：`tests/unit/test_startup_does_not_block_readiness.py`（5 条：AST 不许 `await` 这两步、
  必须交给具名后台任务、不许裸 `create_task`、**行为**判据"替身睡 2.5 s 时首请求必须 2 秒内返回
  且替身确实被执行"、`_bg_run` 运行期间登记/结束注销）。
* **残留**：其余 13 个后台任务实测只造成 **ms 级空档**（启动段最大 45 ms），未逐个错峰；
  启动命令 11.2 s 的大头是多次 PowerShell 进程枚举（每次 ~1.1 s）。

---

## B. 进程与运维（5 条）

### B1 · 拆分会**自动松开两根绳子**（这次改动最大的隐性代价）

* **现象**：把 4 个重作业挪出在线进程后，"重作业有没有在跑"与"服务活不活"**不再等价**。
* **根因（机制）**：拆分前有两个**自动成立**的等式：
  ① **进程活着 ⇔ 作业在跑**（同一个进程）；② **一次只有一个实例**（就一个进程）。
  拆开后：worker **没有端口** ⇒ 它死了前端照旧全绿，而那 4 个作业**永远不再执行**；
  而"起两次"变成重作业**双跑**（含往 14 GiB 行情仓 `upsert` 双写，`CHG-0087` 的同一形状）。
* **修法**：**同一个改动里**答掉两问：
  ① 可见性 = 心跳文件（**守护线程**写，20 s/次，陈旧阈值 90 s，原子替换）+
  `/health` 的 `worker` 段 + 启动横幅**三处**；值守 `ensure` 每分钟确认；
  ② 独占性 = **操作系统级**排他锁（`flock` / `msvcrt.locking`：锁属于打开的文件句柄，
  进程怎么死都由 OS 释放 ⇒ **不存在"陈旧锁"**；拿不到锁要**报出持有者 PID**）。
* **判据**：`tests/unit/test_worker_liveness.py`（三态可区分 · 损坏内容说"读不懂"而非"死了" ·
  阈值 ≥3× 间隔 · **心跳由守护线程写且第一拍立刻落盘** · 第二个锁拿不到且能说出持有者 ·
  锁字节在内容区之外 · **内容写不进去时锁仍算拿到**）+ `tests/unit/test_scheduler_role_split.py`
  （`ensure` / `start` / `stop` / `restart-pilot` 四条接线各一条）。
* **证据**：worker 由值守**自己**拉起（事故流水 `{"event":"worker_restart","reason":"重作业无人执行…"}`）；
  第二实例被拒（日志逐字报出持有者 PID 与启动时刻）。
* **残留**：Windows 计划任务**尚未**单独给 worker 建（靠每分钟 `ensure` 兜住，已验证有效）；
  worker 崩溃**没有**内存/线程栈现场；worker 上没有 `loop_lag`。

### B2 · 触发路径**不止一条**：角色过滤被 `trigger()` 旁路

* **现象（潜在）**：启动自检 `_check_quant_sync_at_startup()` 用
  `scheduler.trigger("quant_data_sync")` 补行情缺口 —— 而那是 `HEAVY_JOBS` 之一。
  第一版拆分只改了 `_tick()` 走的 `schedulable_jobs()` ⇒ **这条旁路原样绕过整次拆分**，
  在**在线进程**里把重作业跑起来（症状与拆分前**一模一样**，而排查者会以为"已经挪出去了"）。
* **根因**：**"谁能跑"的判据只写在一条触发路径上**（与 `CHG-0087` 那次"绕过 `_tick`"同形）。
* **修法**：抽出**唯一**判据 `job_out_of_role()`，`schedulable_jobs()` /
  `scheduler_scope_report()` / `trigger()` **三处同源**；被拦时记 `skipped`（**不是 failed**）并带"由谁跑"的理由。
* **判据**：`tests/unit/test_scheduler_role_split.py::test_trigger_refuses_a_job_outside_the_role`
  （断言**执行器一次都没被调用**）+ 反面 `::test_trigger_inside_the_role_still_runs`。
* **纪律**：新增一道"谁能跑"的判据时，先把**所有触发路径**列出来逐条接上。

### B3 · 优雅停止**从来没生效过**（CTRL_BREAK 到不了守护进程）

* **现象**：重启窗口 38.2 s 里，**停止段占 24.9 s** —— 白等满 20 s 超时。
* **根因**：`request_graceful_stop` 走 `os.kill(pid, CTRL_BREAK_EVENT)`，而它要求
  **调用方与目标共享控制台**；本项目所有守护进程都是 `CREATE_NO_WINDOW` 起的、
  值守又跑在计划任务里 ⇒ 实测**恒返回 False** ⇒"请求优雅退出"什么都没发生，
  而后面"等端口空"的循环因此白等（**端口根本没人去关**）。
* **修法**：**用返回值**（没人接就只给 1 秒缓冲直接树杀）+ 等待判据从"所有目标进程都死"
  改成「**端口空了 + worker 走了**」（`manage.py` 经 uv 起进程会留一个长期存活的外壳，
  按"全部死亡"等会白等）+ 给 worker 补一条**不依赖控制台**的通道：**停止文件握手**
  （`<scheduler_dir>/worker.stop`，worker 用 5 s 轮询的协程消费它，走既有 `_shutdown`）。
* **判据**：`test_stop_file_newer_than_process_triggers_shutdown` ·
  **`test_stale_stop_file_never_kills_a_fresh_worker`**（不变量：**旧文件不许杀新进程**）·
  `test_stop_file_lives_next_to_the_lock_and_heartbeat` ·
  `test_stop_backend_processes_hands_the_worker_a_stop_file`。
* **证据（生产日志逐字）**：`15:52:51 收到停止文件 …（比本进程新）⇒ 开始优雅关停` +
  `worker 关停：释放常驻 SQLite 连接 0 个，checkpoint 主库 1 个`。停止段 **24.9 → 5.8 s**。
* **残留**：`request_graceful_stop` 对守护进程仍是无效路径（保留它只作"万一有控制台"的兜底）。

### B4 · 运维入口写在 `scripts/` ⇒ **等于没交付**

* **现象**：精确重启工具第一版是 `scripts/_restart_pilot_precise.py`。
* **根因**：`.gitignore:97` 的 `/scripts/*` 让**所有**新脚本都不入库 ⇒
  运维入口留在仓库外；而且第一版只读 **PID 文件** ⇒ ① `backend.pid` 里的进程早没了（外壳退出后
  PID 文件不更新）；② PID 文件里的 worker 是 **uv 的 `python.exe` 外壳**，真解释器是它的**子进程**
  ⇒ 给外壳发 CTRL_BREAK **到不了**子进程 ⇒ 把 worker **硬杀**（不 checkpoint）。
* **修法**：实现为 `manage.py restart-pilot`（目标取**并集**：PID 文件 ∪ 端口监听者 ∪
  监听者父进程 ∪ 命令行枚举到的 worker），并加分相位计时（`⏱` 停止段/端口释放/启动命令）。
* **判据**：`test_restart_pilot_is_registered_and_scoped`
  （源码里**不许出现** `stop_backend_processes` —— 复用全量停止更省事，而症状是把 dev 静默停掉）+
  `test_stop_file_paths_are_resolved_per_target_env`。
* **残留**：启动命令 11.2 s 的大头仍是命令行枚举（每次 ~1.1 s），再压需换掉枚举方式（本轮没做）。

### B5 · "没有人在跑"这个判断本身会**抖动**并报假警报

* **现象**：机器负载高时（当时正跑全量测试）命令行枚举返回空 ⇒ `ensure` 判成"没在跑" ⇒
  白起一个 worker：① 被单实例锁挡下（退出码 3，**锁救了它、没有双跑**）；
  ② 但日志留下 ERROR、值守还报了一条「启动后立即退出 ⇒ 重作业仍无人执行」的**假警报**。
* **根因**：**单路仪器**（PowerShell CIM 查询会超时）。
* **修法**：`worker_present()` 用**三路独立证据**（进程枚举 ∪ 单实例锁 ∪ 心跳），
  任一成立即算"在"；并且"枚举没看见但锁/心跳说在"时**明说第一路仪器这次不可靠**（仪器降级要留痕）；
  拉起后若新进程立刻退出，**再问一次**，锁说有人持着就返回 0（不许报假警报）。
* **判据**：`test_worker_present_has_three_independent_evidences`（含反向"三路都没有必须判不在"）+
  `test_ensure_worker_does_not_cry_wolf_when_lock_says_alive`。
* **残留**：三路证据仍是"尽力而为"；若三路同时失效且实例确实死了，会出现最多 1 分钟的静默窗口。

---

## C. 出口与协议（2 条）

### C1 · 给非 WebSocket 路径发升级请求 ⇒ **500**（应 404/1008）

* **现象**：`/api/v1/health/live` 带升级头、或任意**不存在的** WS 路径 ⇒
  `starlette/staticfiles.py:91` 的 `assert scope["type"] == "http"` 抛 `AssertionError`
  ⇒ uvicorn 记 `connection rejected (500 Internal Server Error)`。
* **根因**：SPA 静态资源挂在 `/`（`Mount("/")` 匹配**任何**路径，含 WebSocket 作用域）。
* **修法**：包一层 5 行 ASGI shim `_HttpOnlyStatic`（HTTP 透传；WS 有「拒绝响应」扩展回 **404**，
  否则 accept 前 `websocket.close(1008)`；lifespan 等不参与）。
* **判据**：`tests/unit/test_ws_scope_guard.py`（5 条：三种"没有 WS 路由"的路径必须 404/1008
  且**不能**是 500 · 静态挂载仍服务 HTTP · shim 合同"HTTP 透传、WS 不进内层、扩展缺失时 close(1008)、
  lifespan 不参与"）。
* **证据（公网实测）**：非 WS 路径+升级头 ⇒ **404**（原 500）；真实 WS 路径 ⇒ **403 不变**（登录门槛）；
  `/health/live`、`/healthz`、`/` ⇒ 200。★ **404 与 403 必须能区分**，否则"WS 路由被误删"会伪装成"反正都是拒绝"。
* **残留**：无（该路径已闭环）。

### C2 · 出口护栏漏了 numpy 标量 ⇒ **三种症状**（一个 500 + 一个类型漂移 + 另一个 500）

* **现象**：`np.float32('nan')` / `np.float16('nan')` / `np.float32('inf')` 原样漏到 `json.dumps`
  ⇒ `ValueError: Out of range float values are not JSON compliant` ⇒ **整响应 500**；
  `np.int64/int32/uint8(7)` 被压成 `7.0` ⇒ **类型漂移**；`np.bool_(True)` 既非 `bool`
  也非 `numbers.Real` ⇒ 原样返回 ⇒ `TypeError: not JSON serializable` ⇒ 又一个 500。
* **根因（一个）**：`np.float32` **不是** `float` 的子类（`np.float64` 才是）⇒ 走不到
  `isinstance(value, float)` 分支；而兜底的 `Real` 分支**没有非有限判断**
  （`round_significant` 的契约是"非有限原样返回"）。
* **修法**：`Real` 分支补 `math.isfinite` ⇒ 非有限给 `None`；`Integral` 保持 `int`；
  新增 `.item()` 归一分支（numpy 标量转原生标量后**重走一遍本函数**）
  ⇒「非有限→None / 整型保持 int / 布尔保持 bool」三条规则**只写一遍**。
* **判据**：`tests/unit/test_safe_json_response.py::test_numpy_scalars_are_normalized_to_native_types`
  （**类型断言**是重点：`7 == 7.0` 与 `True == 1` 都为真，只查值会放过"整数被压成浮点"）。
* **证据**：11 种 numpy 标量逐个过（nan/inf 各族 → None；`np.float32(2.5)` → 2.5；
  `np.int64/32/uint8` → **int**；`np.bool_` → **bool**）。
* **残留**：同类漏网可能还有别的第三方标量类型（按需再补，判据形态已就位）。

---

## D. 我的协作缺陷（自伤 5 条，全部已机器化）

### D1 · **单测把在跑的 dev 实例停掉了**（真实生产影响）

* **现象**：四条只 monkeypatch 了后端枚举的用例，枚举到**真实 PID**；其中一条 patch 了
  `kill_pid_tree` 却**没** patch `request_graceful_stop` ⇒ 向真实进程发 CTRL_BREAK ⇒
  **dev（8100）被停掉**（事故流水 14:32/14:34/14:37 连续三次 restart），pilot 也收到信号，
  worker **差一步**被停（断言先红）。
* **根因**：**测试与生产跑在同一台机器上**，而"记得 patch"不是护栏。
* **修法（机器强制）**：`tests/conftest.py::_forbid_real_process_signals`（autouse）——
  **目标 PID 真的活着就拒绝执行**并返回 False；**只拦活着的 PID** 是关键
  （故意验证真实实现的用例传的是假 PID，应继续走真实分支）。
* **判据**：`tests/unit/test_test_harness_isolation.py`（3 条，含"直接调夹具本体"与反向判据）。

### D2 · 用例把**别的环境**的配置缓存进进程 ⇒ 后面 9 条判据假红

* **现象**：`test_scheduler_role_split.py` 的夹具设 `MOSS_ENV=pilot` 后，
  `heartbeat_path()` 回落到 `get_settings()`（`@lru_cache` 单例）⇒ 一份 **pilot** 的 Settings
  被留在进程里 ⇒ 之后的 `/health` 用例被当成**公网实例**（登录门槛）**全部 401**，
  而它们**单独跑全绿**。
* **根因**：**进程级单例 + 测试共享进程**。
* **修法**：该夹具同时重定向 `SCHEDULER_DIR`（不再触到 Settings）+
  `tests/conftest.py::_reset_settings_cache`（autouse：每个用例前后 `get_settings.cache_clear()`）
  —— 把"记得清缓存"（此前只有 14 个文件各自记得）升级为**全局不变量**。
* **判据**：`tests/unit/test_test_harness_isolation.py::test_settings_cache_clearer_actually_clears`
  （**不看执行顺序**：直接调夹具本体）。

### D3 · **形状判据**两个方向都错过（假绿 + 假红）

* **假绿**：在飞登记依赖用 `api_router.dependencies.append(...)` 挂上，判据断言"它在列表里" ✅
  —— 而 FastAPI 的 `include_router` **只应用调用时传进来的** `dependencies=`，事后 append **不生效**：
  请求跑完全程、登记簿一条都没有，卡顿日志永远印「（无在飞请求）」。
* **假红**：把裸 `create_task` 统一包成 `_bg_run(name, coro)` 之后，
  "必须交给具名后台任务"的 AST 判据**报红**（它按形态找 `create_task`，包装后那层不在原地）。
* **修法/判据**：判据一律改成**行为**（`test_router_registration_is_effective_not_just_attached`
  真发请求断言登记被调用；`test_startup_does_not_block_readiness` 同时接受两种形态但要求
  "这个重活被交出去了"）。
* **纪律**：**判"接线"要看电流，不要看电线在不在。**

### D4 · 判据/路径在**父进程环境**里求值（同一坑第 3 次）

* **现象**：停止 worker 的**停止文件路径**在父进程里解析 ⇒ 写到 `data/scheduler/`，
  而 pilot 的 worker 看 `data/pilot/scheduler/` ⇒ **信号发到另一个目录、两边都静默**
  （命令还印「已写停止文件」）。
* **根因**：`--env pilot` 只是命令行参数，父进程没有 `MOSS_ENV` / `SCHEDULER_DIR`
  （与历史 `CHG-0112` 横幅、我新写的 `status` worker 行**同形**）。
* **修法**：`_worker_stop_files(env)` 在**目标环境的变量下**解析（`_temporary_environ` 包住）；
  `cmd_stop` 的全量版本覆盖**所有已知环境**。
* **判据**：`test_stop_file_paths_are_resolved_per_target_env`。

### D5 · 长中文写进代码字面量 ⇒ 同类自伤 **11 次**

* **现象**：ASCII 双引号混进同样用双引号定界的中文串 ⇒ 语法错误（整文件导入失败，
  而 `invalid character` 从不指向真因）或半角引号文案。
* **修法（三步）**：① 中文引号一律用「」；② 全库解析判据
  `tests/unit/test_cjk_quote_safety.py`（断言**我们自己写的每个 `.py` 都能被 AST 解析**，
  强制 `utf-8-sig` 读）；③ **长文案不进代码字面量**（外置 `.md` 载荷 + 脚本读取）。
* **附带纪律**：那次自伤能发生，是因为临时脚本写在 `data/`（判据按设计跳过）⇒
  **把文件写到了判据覆盖范围之外**；此后探针/临时脚本一律放 `scripts/`。

---

## 5. 三条「以点带面」的机制（不是逐条修的）

| # | 机制 | 一句话 | 落点 |
|---|---|---|---|
| 1 | **仪器先行 + 能指名** | 先让"卡了"能说出"**谁卡的**"，再决定修谁；否则只能靠猜 | `src/core/loop_lag.py`、`src/core/inflight.py`、`tests/integration/test_no_loop_blocking_endpoints.py` |
| 2 | **判据必须观测后果** | 形状判据（结构在不在）会**双向**出错；行为判据才对 | 见 D3 的三条判据 |
| 3 | **纪律机器化** | "记得 patch / 记得清缓存 / 记得清引号"都不是护栏；autouse 夹具 + 解析判据 + 自证才是 | `tests/conftest.py`、`tests/unit/test_test_harness_isolation.py`、`tests/unit/test_cjk_quote_safety.py` |

---

## 6. 全项目问题清单索引（不重复，只指向）

| 文档 | 覆盖 |
|---|---|
| `docs/SESSION_PROBLEMS_AND_ROOTCAUSE_20260930.md` | 同日**另一轮**（日线链原生崩溃 / V8 并发 / 拥挤度库归属等） |
| `docs/SESSION_PROBLEM_INVENTORY_20260930.md` | 同日另一轮的简要清单 |
| `docs/PRD.md` §19.34–§19.42 | 各条口径的**现行事实源**（含判据与实测表） |
| `docs/REQUIREMENT_CHANGELOG.md` | 逐条 CHG 登记（本轮：`CHG-0140`/`0141`/`0146`/`0147`/`0148`/`0150`） |
| `docs/PROBLEM_INVENTORY_20260926.md`、`docs/PROJECT_AUDIT_2026-09-27.md` 等 | 更早轮次的问题清单 |
| `.trae/skills/ai-defect-and-misread-guard/SKILL.md`（§6.7 / §6.8 / §8 / §9） | 本文件 §D 那 5 类的**通用纪律**（下次怎么避免，含开工前/收尾清单）。★ 本会话原先的平行文件 `ai-self-harm-machine-guard` 已**并库**到这里（那个文件只留入口与去向表），避免同一判断出现两个事实源 |

---

## 7. 本轮「没量到 / 未覆盖面」（诚实登记，不许省略）

1. **B 类阻塞（启动/后台任务）只治了最贵的两段**；其余 13 个后台任务只量到"ms 级空档"，未逐个错峰。
2. **未覆盖带必填参数的端点与 POST**：`gap_max_ms` 判据只扫无参 GET。
3. **线程池打满后的行为未实测**：搬进 `to_thread` ≠ 不再排队（只是不再堵事件循环）。
4. **worker 崩溃无现场**（无内存/线程栈）；worker 上没有 `loop_lag`。
5. **Windows 计划任务未给 worker 单独建**（靠每分钟 `ensure` 兜住）。
6. **备用 CF 链路结构性劣化未修**（首页 403；主用正常时不影响客户）。
7. **体检的带宽数字是单次采样**（本机 16,605 KB/s、主用 133.2 KB/s），未建立持续测量。
