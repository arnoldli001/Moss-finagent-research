# 项目问题清单 · 根因 · 解决方案（Moss-FinAgent-Research）

> **本文的口径**：只写**有证据**的东西 —— 每一条都带「症状（用户视角）／根因（机制）／修法／判据（可复跑的测试名）／实测数字／遗留」。
> 没有量到的写「没量到」，不用「应该没问题」占位（本项目硬约束：**没量到 ≠ 量到 0**）。
>
> **范围**：2026-09-28 ~ 09-30 的连续会话（含并发协作者的工作），重点是与**在线服务可用性 / 数据正确性 / 交付纪律**相关的部分。
> 归属说明：标「我」= 本会话主责；标「协作」= 同仓库并发会话主责，我参与验证或收口。
>
> 证据坐标：`docs/PRD.md` §19.30–§19.42 · `docs/REQUIREMENT_CHANGELOG.md` CHG-0132…CHG-0150 · `data/run/backend.log` · `data/pilot/access_audit/access_audit.jsonl` · `data/pilot/scheduler/runs.jsonl` · `tests/` 下各判据文件。

---

## 0. 一页速览（按"后果类别"排序）

| # | 问题类 | 最贵的一次症状 | 根因一句话 | 状态 |
|---|---|---|---|---|
| A | **事件循环争用** | 用户三小时内三次看到「后端不可达」；请求最长 **64.4 秒** | 单进程单循环里，批作业/同步重活/启动重活都压在同一个循环上 | ✅ 三类全修（A1/A2/A3） |
| B | **拆分的代价**（新引入） | 重作业可静默停摆、或双跑写同一个库 | 进程拆开后，"活着"与"在跑"不再是同一件事 | ✅ 心跳+锁+三路接管+旁路堵死 |
| C | **可观测性缺口** | 「异常记了、界面上查无此项」「卡了但说不出谁卡的」 | 判断在错的层求值 / 观测没有名字 | ✅ 点名式归因落地 |
| D | **测量与归因失效** | 按 `latency_ms` 排序把**受害者**排成头号犯人 | 代理指标把"排队"算成了"成本" | ✅ 仪器换层 + 可复现重放 |
| E | **出口护栏与作用域边界** | `np.float32('nan')` 让整响应 500；给 WS 路径发升级头也 500 | 护栏只覆盖了一类输入 / 横切改动没枚举作用域 | ✅ 各补一条类级判据 |
| F | **测试台架自身的缺陷** | 单测**对真实进程发停止信号**，把线上 dev 停掉 | 夹具只管自己一格，而进程/环境是全局的 | ✅ 机器强制 + 全局清理 |
| G | **交付与协作纪律** | 生产代码未入库（克隆里少 57 个护栏）；CHG 号连撞三次 | "我改了"≠"它发布了"；共享文件用"开始时读一次" | ✅ 判据 + 流程 |
| H | **数据采集正确性** | 招行股息率"缺失"（其实采到了） | 匹配侧没接同义词池；能力登记不全 | ✅ 语义扩张 + 能力补登记 |
| I | **公网链路与带宽** | 「十几秒才出数据」 | 载荷体积 × 隧道带宽，不是"后端慢" | ✅ 体积治理 + 体检脚本 |
| J | **遗留未闭环** | —— | —— | ⏳ 见 §10 |

---

## A. 事件循环争用（同一根因，三种入口）

**统一根因**：uvicorn 单进程单事件循环。任何**同步**重活（不管是定时作业、请求处理器，还是启动段）都会**按住整个循环** ⇒ 同一时刻所有人的请求一起排队。前端存活探针超时 **4 秒**，所以"循环被按住 >4 秒"在界面上就等于「后端不可达」——**与进程真的死了长得一模一样**。

### A1 批作业占用（最大的一块）

| 项 | 内容 |
|---|---|
| 症状 | 三小时内三次「前端后端不可达」；重启后依旧 |
| 证据 | `access_audit.jsonl`：13:54–13:59 内最高 **64,393 ms**；13:58 那一分钟 **23 条请求里 22 条 >1 秒**；**全部最终 200**（是排队，不是报错） |
| 根因 | `runs.jsonl` 1847 条真实记录里，4 个**纯后台**作业累计 ≈ **50,000 秒**：`intel_tone_extract` 24,849 s（中位 304 s、最大 **2,037 s**）、`event_alert_intraday` 13,201 s、`quant_data_sync` 5,943 s、`mainline_daily` 5,894 s（中位 **2,947 s**）。它们对实时性零要求，却与在线请求共享循环 |
| 修法 | ① `daily_warm` 软超时 → **硬预算**（`DAILY_WARM_BUDGET_SEC=90`、每只 `PER_CODE_SEC=12`、并发 3）：单轮 **316.4 s → 19.8 s**，失败 22 → 3；② 把 4 个重作业**移出进程**（见 B） |
| 判据 | `tests/unit/test_daily_warm.py::test_round_wall_clock_is_hard_capped_by_budget`（20 只挂死 + 预算 0.5 s ⇒ 必须 3 秒内返回）· `::test_budget_hit_records_an_anomaly` · `::test_warm_concurrency_is_justified_by_measurement`（并发值必须由实测表支撑——我把它调到 5 时**被这条当场打回**） |
| 验证 | 拆分后第一个真实班次：`quant_data_sync` **16:00:03 在 worker 里跑 585 秒**；同一窗口 pilot 服务 **55 个请求，max 14 ms、p95 1 ms、>1 秒 0 条** |

### A2 请求路径阻塞（async 处理器里做同步重活）

| 项 | 内容 |
|---|---|
| 症状 | 面板偶发"卡住不动"；某些接口首次打开特别慢 |
| 根因 | FastAPI 语义：`def` 处理器会被自动丢线程池，**`async def` 里做同步重活则直接在循环上跑**。实测 4 个端点：`auction_select/sentiment_cycle` 堵循环 **3,056.8 ms**、`mainline/data/status`（冷）**3,766.7 ms**、`mainline/relevance` 679.1 ms、`auction_select/preheat` 179.7 ms |
| 修法 | 一律改成仓库既有范式 `await asyncio.to_thread(...)`；`relevance` 的同步主体抽成 `_relevance_stats_sync()` 后整体入池 |
| 判据 | `tests/integration/test_no_loop_blocking_endpoints.py`（**行为**判据：进程内 20 ms 心跳量 `gap_max_ms`，含四个已修端点的**回归锁** + **自证**：同步睡 400 ms 必须被抓到、`await sleep` 必须不被误判）· `tests/unit/test_api_no_loop_blocking.py`（AST 守卫，**从只扫 `quant.py` 扩到全部路由文件**） |
| 验证 | 修后复扫 **107 条无参 GET：堵循环 ≥200 ms 的端点 2 条 → 0 条**（最坏剩 167 ms，是一个 debug 端点）。★ 注意区分：`quant/data-status` 一次 10 秒、`mainline/snapshot` 一次 14 秒，但**只堵 12 / 116 ms**（早已在线程池里）——"慢"与"堵"是两件事 |

### A3 启动段挡住"就绪"

| 项 | 内容 |
|---|---|
| 症状 | 每次重启后一段时间内前端必然"不可达"（客户以为服务挂了） |
| 根因 | `lifespan` 里 `await cat.rebuild_all()`（含对 **1038 个指标**做频率推断，日志时间线上就是 **16 秒**的间隔）+ `await asset_cat.scan_and_store()`。而 uvicorn **先绑端口、再跑 lifespan** ⇒ 端口在监听、请求却排在启动完成之后 ⇒ 探针必超时。★ 而 `loop_lag` **看不见这一段**（监控在它之后才启动）——「卡顿表里没有它」≠「它不卡」 |
| 修法 | 两段本来就跑在 `to_thread`（不占循环）⇒ 问题不是"阻塞循环"而是"**谁在等它**"：改成具名后台任务 `_rebuild_catalog_and_assets_later`（体内 try/except 一字不差），并登记进在飞簿 |
| 判据 | `tests/unit/test_startup_does_not_block_readiness.py`：AST（不许 `await` 这两步、必须交给具名后台任务且**不许裸 `create_task`**）+ **行为**（把重建换成"睡 2.5 秒"的替身 ⇒ 首请求必须 2 秒内返回**且替身确实被执行**——只测前者会奖励"删掉这一步"，只测后者会奖励"改回 await"） |
| 验证 | 启动到就绪 **~20 秒 → 0.6 秒**；后台两段照常完成（`元数据 1246 条 / 回填 2233 个指标`、`210 个资产 / 147,532,584 行`），且都在**就绪之后** |

---

## B. 进程拆分的代价：两类**新的静默失效**（我主责）

**统一根因**：把原先**绑在一起**的两件事拆开，必然松开两根绳子 ——
① 「进程活着 ⇔ 重作业在跑」失效（worker **没有端口**，它死了前端照旧全绿）；
② 「一次只有一个实例」失效（起两次 = 重作业**双跑**，含往 14 GiB 行情仓双写，即 `CHG-0087` 那次事故的形状）。

| 子问题 | 根因细节 | 修法 | 判据 |
|---|---|---|---|
| B1 **重作业静默停摆** | worker 无端口、无前端入口 | 心跳文件（**独立线程**写！见下）+ `/health` 的 `worker` 段 + 启动横幅 + `manage.py status` 行 | `tests/unit/test_worker_liveness.py`（三态可分：不存在/新鲜/陈旧 + 「内容无法解析」第四态）· `test_manage_cli.py::test_banner_truthfully_says_worker_missing` |
| B2 **心跳不能写在循环里** | worker 的循环**就是**重作业跑的地方（`mainline_daily` 中位 2,947 s）⇒ 环内写心跳会把**正在干活**的 worker 判成**死的**并重启它（"把忙判成死"比不监控更糟） | 心跳写在**守护线程**里：与作业是否在跑完全无关；写间隔 20 s、陈旧阈值 90 s、**原子替换**（读到半个 JSON 会被读成"死了"） | `test_worker_liveness.py::test_heartbeat_is_written_by_a_dedicated_thread`（含"第一拍必须立刻落盘"） |
| B3 **双跑** | 起两次 worker（手滑/值守与人工撞车） | **操作系统级排他锁**（`flock`/`msvcrt.locking`）：锁属于文件句柄，进程怎么死都由 OS 释放 ⇒ **不存在"陈旧锁"**；拿不到锁**必须报出持有者 PID** | `test_worker_liveness.py::test_second_lock_cannot_be_acquired` · 锁字节**刻意放在内容之外**（偏移 4096）——Windows 字节锁**连读一起拒**，锁在 0 就说不出"是谁在跑" |
| B4 **值守/停止/启动没接管** | 拆出来后没人保证它在跑、也没人保证停止时把它带上 | ① `manage.py ensure`（唯一值守入口，每分钟一轮）后端健康时**也要**确认 worker；② `start --env pilot` **连带**拉起；③ `stop`/`--replace` **必须**停掉它（否则留孤儿写者）；④ 新增 `restart-pilot`（只动 pilot，不碰 dev） | `tests/unit/test_scheduler_role_split.py`（`ensure`/`start`/`stop` 三条接线各一条判据）· `test_manage_process_lifecycle.py::test_stop_backend_processes_also_stops_workers` |
| B5 **旁路绕过整次拆分** | `SchedulerService` 有**两条**触发路径：`_tick()` 与 `trigger()`（启动自检补偿）。只改了前者 ⇒ `_check_quant_sync_at_startup()` 用 `trigger("quant_data_sync")` 把重作业**又拉回在线进程** | 抽出**唯一**角色判据 `job_out_of_role()`，三处同源调用；`trigger()` 被拦记 `skipped`（不是 failed）+ 理由 | `test_scheduler_role_split.py::test_trigger_refuses_a_job_outside_the_role`（★ 断言**执行器一次都没被调用**，不看返回值） |
| B6 **停止 worker 的通道** | `CTRL_BREAK` 要求调用方与目标**共享控制台**，而守护进程全是 `CREATE_NO_WINDOW` ⇒ 实测**恒返回 False** ⇒ 只能硬杀，而硬杀**不 checkpoint** | 补**停止文件**握手（5 s 轮询），并立不变量「**旧文件不许杀新进程**」（mtime 晚于本进程启动才算） | `test_worker_liveness.py`（新文件触发/陈旧忽略且顺手删/与锁心跳同目录）· `test_manage_process_lifecycle.py::test_stop_backend_processes_hands_the_worker_a_stop_file` |
| B7 **"没在跑"也会误判** | 只用命令行枚举（PowerShell CIM）判存活，机器负载高时枚举返回空 ⇒ 白起一个 worker + 报假警报 | 三路独立证据：**进程** ∪ **单实例锁**（OS 级、不抖动）∪ **心跳**；且"枚举没看见但心跳/锁说在"时**明说第一路仪器不可靠** | `test_scheduler_role_split.py::test_worker_present_has_three_independent_evidences` · `::test_ensure_worker_does_not_cry_wolf_when_lock_says_alive` |

**验证（生产）**：`worker_restart` 事故流水 + 值守自动拉起 + 心跳 beats 递增 + 日志逐字 `调度 worker 启动：env=pilot role=worker 本进程负责 4 个作业`；停止时 `收到停止文件 ⇒ 开始优雅关停` + `checkpoint 主库 1 个`。

---

## C. 可观测性缺口：**谁能看见**（我主责）

| 子问题 | 根因 | 修法 | 判据 |
|---|---|---|---|
| C1 「不跑」与「没人跑」读起来一样 | 只报"已由 worker 负责"是**半个结论** | 横幅/`status`/`/health` 三处都说"worker 在不在"，并把**心跳年龄**印出来 | `test_manage_cli.py::test_pilot_scope_line_states_worker_status` |
| C2 **假绿措辞** | 陈旧阈值 90 s ⇒ 一个**已死 86 秒**的 worker 仍判 `alive=True`，横幅照旧印「**在跑**」 | 增加 `fresh`（≤2 个写间隔）+ 措辞分三档：**刚刚 / 偏旧 / 未在运行** | `test_manage_cli.py::test_banner_does_not_call_a_stale_heartbeat_running`（**两半都要**：86 s 必须说"偏旧"、5 s 必须说"在跑"——只测一半会奖励"一律说偏旧"，那样的判据会被当噪音关掉） |
| C3 **判据在错的环境里求值**（同一个坑三次） | ① 横幅读父进程 `os.environ`（`--env pilot` 只是命令行参数）⇒ 印出与事实**相反**的结论；② `status` 的 worker 行同理（恒为"本实例不需要"）；③ 停止文件路径同理（发到另一个目录，两边都静默） | 一律在**即将生效的那份环境**里求值（`_temporary_environ`），并在文案里点名环境 | `test_manage_cli.py`（横幅自证档位）· `test_scheduler_role_split.py::test_stop_file_paths_are_resolved_per_target_env` |
| C4 **仪器要有人读** | 裁剪理由曾只写在 `logger.info`，而仓库没有 `basicConfig` ⇒ INFO 被丢弃（逐字节搜「调度器已启动」= 0 处） | 挂到 `/health`（前端 20 秒轮询的既有面，零新增往返） | `tests/unit/test_warehouse_write_ownership.py::test_health_exposes_schedule_scope_contract` |
| C5 **卡了但说不出谁卡的** | `loop_lag` 只说"循环卡了多久" | 新增**在飞登记簿**（`src/core/inflight.py`）：卡顿**那一刻**读"谁在执行"，日志变成 `…卡顿时刻在飞：GET /api/v1/fundflow/snapshot(2641ms)`；**没有**请求在飞时输出「（无在飞请求）」（那同样是结论：说明卡顿来自后台） | `tests/unit/test_inflight_registry.py`（6 条，含"无在飞时 describe 为空串"）· 17 个后台任务全部具名登记（`_bg_run`） |
| C6 多天混合编码的日志 | `data/run/backend.log` 整体 utf-8 / gbk **都无法解码** | 逐行试探解码 + 永远带日期（本文档的所有日志引用都按这个口径读） | 见 `scripts/` 下探针的 `for enc in ('utf-8','gbk')` 兜底 |

---

## D. 测量与归因失效（我主责，**三轮才收敛**）

| 轮次 | 用的仪器 | 得到的"头号犯人" | 为什么错 |
|---|---|---|---|
| ① | `access_audit.latency_ms` 排序 | `/research/{task_id}`（p50 **8.3 秒**、184 次） | 它只是**内存字典查表**。它慢是因为研究期间前端**轮询它最频繁** ⇒ 总在队列里，把**别人的阻塞**记在自己账上（被堵时几十个路径同时放行，出现一批一模一样的 47708/47757/47754… ms） |
| ② | `loop_lag` + 在飞点名 | `fundflow/snapshot`（卡顿 1,516 ms 时它已飞 2,641 ms） | 只是**共现**：聚焦重放 6 次全是 120–210 ms、**不复现** ⇒ 那次是**冷路径** |
| ③ | **进程内 harness**（同循环 + 20 ms 心跳） | 见 A2 的四个端点 | ✅ 确定性、可复现，直接当判据 |

**沉淀成纪律**：
* `latency_ms` 里混着**排队**时间 ⇒ 按它排序会**系统性地把头号受害者排在第一个**；
* 要区分"谁堵的"与"谁在排队"，必须在**卡顿那一刻**看"谁在飞"；
* 要**定罪**必须能**可复现地重放**（共现 ≠ 因果）；
* **在错的层上测量**是一类独立缺陷：`handler` 里耗时恒为 ~0 ms（被堵的请求**进不到 handler**）、心跳写在循环里会把"忙"判成"死"、`inspect.getsource` 按行号取源码在文件被改后会取到**别的函数**。

**另一类**：**形状判据假绿** —— 我在飞依赖挂在 `api_router.dependencies` 上并断言"它在列表里"，判据绿了，而 FastAPI 的 `include_router` **只应用调用时传进来的** `dependencies=`，事后 append **不生效** ⇒ 请求跑完全程、登记簿一条都没有。修法：改到唯一挂载点 + 判据改成**行为**判据（真发一个请求、断言登记被调用）。**判"接线"要看电流，不要看电线在不在。**

---

## E. 出口护栏与作用域边界（我收口，缺陷源在协作侧）

| 子问题 | 根因 | 修法 | 判据 |
|---|---|---|---|
| E1 `np.float32('nan')` 让整响应 **500** | 出口护栏 `round_payload` 只覆盖 `float`/`int`/`bool`，而 **`np.float32` 不是 `float` 子类**（`np.float64` 才是）⇒ 走不到 float 分支；兜底的 `Real` 分支**没有非有限判断**（`round_significant` 契约是"非有限原样返回"） | `Real` 分支补 `math.isfinite` ⇒ 非有限 → `None` | `tests/unit/test_safe_json_response.py::test_safe_response_handles_numpy_scalars` |
| E2 同一类的另外两个形态 | `np.int64(7)` 被压成 `7.0`（**类型漂移**：成交量/条数/根数变浮点）；`np.bool_(True)` 既非 `bool` 也非 `Real` ⇒ 原样返回 ⇒ **`TypeError` 又一个 500** | `Integral` 保持 `int`；新增 `.item()` 归一分支（numpy 标量转原生标量后**重走一遍本函数**）⇒ 三条规则**只写一遍** | `::test_numpy_scalars_are_normalized_to_native_types`（★ **必须断言类型**：`7 == 7.0` 与 `True == 1` 都为真，只查值会放过漂移） |
| E3 给非 WS 路径发升级头 ⇒ **500** | SPA 静态资源挂在 `/`（`Mount("/")` 匹配**任何**路径），而 `staticfiles.py:91` 第一行是 `assert scope["type"] == "http"` ⇒ 任何 WebSocket 作用域都变 `AssertionError` | 5 行 ASGI shim `_HttpOnlyStatic`：HTTP 透传；WS 有「拒绝响应」扩展回 **404**，否则 accept 前 `close(1008)`；lifespan 不参与 | `tests/unit/test_ws_scope_guard.py`（5 条：三种"没有 WS 路由"的路径必须 404/1008 且**不能是 500** · 静态挂载仍服务 HTTP · **shim 合同**直接对挂载对象断言） |
| E4 **横切改动的作用域** | 给"所有 API 路由"加一个依赖时，忘了路由集合里有 **WebSocket** ⇒ WS 的 scope 没有 `request` ⇒ `TypeError` ⇒ 2 个 WS 判据 + 7 个 health 契约判据一起红 | 依赖改收 `HTTPConnection`（`Request` 与 `WebSocket` 的共同基类） | `tests/unit/test_inflight_registry.py::test_the_dependency_also_works_on_websocket_routes`（独立小应用 + 真 WS 收发） |

---

## F. 测试台架自身的缺陷（我引入、我修）

| 子问题 | 症状（最贵） | 根因 | 修法 |
|---|---|---|---|
| F1 **单测对真实进程发停止信号** | 一次全量运行**把线上 dev（8100）停掉**，pilot 的 uvicorn 也收到信号（侥幸只打到启动器外壳），worker **差一步**被停 | 四条既有用例只 monkeypatch 了"后端枚举"，而 `stop_backend_processes` 新增了 worker 枚举 ⇒ 枚举到**真实 PID**，其中一条 patch 了 `kill_pid_tree` 却没 patch `request_graceful_stop` | ① 四条用例补齐 patch + 新增"worker 也必须被停"的判据；② `tests/conftest.py` 新增 autouse 夹具 `_forbid_real_process_signals`：测试进程内**只要目标 PID 真的活着就拒绝执行**（只拦"活着的 PID"是关键：故意验证真实实现的用例传的是假 PID，应继续走真实分支） |
| F2 **跨用例污染 Settings 缓存** | 四条 `/health` 判据**单独跑全绿、合跑全红**（返回 401） | 我的夹具把 `MOSS_ENV=pilot` 设上后，`heartbeat_path()` 回落到 `get_settings()` ⇒ 一份 **pilot 的 Settings 被 `lru_cache` 缓存进进程** ⇒ 后续用例被当成**公网实例**（登录门槛强制） | ① 该夹具同时重定向 `SCHEDULER_DIR`；② conftest 新增 autouse `_reset_settings_cache`（每个用例前后清缓存）——把"记得清"（此前只有 14 个文件各自记得）升级为**全局不变量** |
| F3 **编辑期间跑套件 ⇒ 假红** | 全量运行中我改了 `manage.py`，`inspect.getsource` 按行号取源码 ⇒ 5 条 tunnel 判据红，而它们读到的"源码"是**别的函数** | 模块在内存里是旧行号、磁盘文件已变 | 纪律：**改文件期间不许跑套件**；跑完再改（复跑同一批文件：108 passed） |
| F4 判据把"形状"当"行为" | 见 D 的"形状判据假绿" | —— | 见 D |

**成品判据文件**（本会话新增，均在 `tests/`）：
`test_scheduler_role_split.py`(279 行) · `test_worker_liveness.py`(183) · `test_inflight_registry.py`(154) · `test_startup_does_not_block_readiness.py`(132) · `test_no_loop_blocking_endpoints.py`(157, integration) · `test_cjk_quote_safety.py`(87) · `test_ws_scope_guard.py`(79) · `test_test_harness_isolation.py`(46)。

---

## G. 交付与协作纪律（我主责 + 收口）

| 子问题 | 根因 | 修法 | 判据 |
|---|---|---|---|
| G1 生产代码/护栏**未入库** | `src/` 27 个条目、`tests/` 57 个条目从未 `git add`，而它们正在被运行中的 pilot 执行。本机全绿、克隆里**少 57 个护栏**——**没有任何判据会报** | `git add`（只认 `??`，不认 ` M`——工作区改了没提交是正常开发状态） | `tests/unit/test_source_tree_is_tracked.py`（含自证：喂混合状态串必须只挑出 `??`；`ls-files` 计数下界防"整棵树被移出索引"） |
| G2 CHG 号**连撞三次** | "会话开始时读一次最大号"⇒ 期间并发协作者又落了号（0142/0144/0149 都被占） | 落笔前**现读**最大号再 +1；改完把引用一起对齐；**长中文文案外置 `.md` 载荷**（见 G4） | `scripts/prd_sync_check.py --ledger`（枚举/形状/证据可复跑） |
| G3 共享文件被写坏 | 覆盖式编辑把台账 `## 4` 标题写丢两次；PRD 标题被吃掉 | 改结构性文件后跑**形状自检**（`^## ` 序列 + `--ledger`） | 同上 |
| G4 **长中文写进 Python 字符串**（我犯 **11 次**） | ASCII 引号混进中文串 ⇒ 整脚本 `SyntaxError`，而报错从不指向真因 | ① 判据 `tests/unit/test_cjk_quote_safety.py`（"我们自己写的每个 `.py` 必须能被 AST 解析"，零误报、对那 11 次 100% 命中；强制 `utf-8-sig` 读，仓库有 3 个 BOM 文件）；② **根治**：长中文文案改成**外置 `.md` 载荷 + 脚本读取** ⇒ 这一类在**载荷层**就不可能发生（后 3 次里 2 次被这条判据在**运行前**拦下） | `test_cjk_quote_safety.py`（5 条，含"历史事故写法必须仍被判死"的自证） |
| G5 交付完整性 | 契约变更没同步判据（我改横幅文案 ⇒ 4 条既有判据红） | 改契约时同步改判据，并在判据 docstring 里写清**为什么改**（避免下一个人以为判据写错了） | 见各判据文件 |

---

## H. 数据采集正确性（我主责，`CHG-0135/0136` 一脉）

| 子问题 | 根因（**三层，都不是"取不到"**） | 修法 | 判据 |
|---|---|---|---|
| H1 招行"股息率缺失"（其实采到了） | ① 数据**采到了**（`股息率TTM:600036` 60 点，最新 2026-09-29，源=本地 quant_daily_basic）；② **白名单匹配没用同义词池**：白名单写中文「市净率/市盈率/股息率」，库里是英文 `PB`/`PE(TTM)`/`dividend_yield`，匹配是**子串** ⇒ `PB:600036` 没有任何 Agent 放行（**采了白采**）；③ **行业股息率能力存在但没登记**：实测 `ind:sw_first_dividend_yield:银行` 真取得到（银行 5.1%、PE-TTM 7.34），但 `get_capabilities()` 只登记了三级 `{行业名}` ⇒ 一级行业（银行是申万一级）路径规划不到 | 复用既有 `synonym_dict`（154 指标别名 + 260 实体别名 + 5568 生成别名）做**检索期语义扩张**；新增**词边界规则**（ASCII 别名按词边界、中文保持子串，防 `pe` 命中 `penetration`）；连接器补登一级/二级 `{行业名}` × 4 指标 | `tests/unit/test_whitelist_semantic_expansion.py`（10 条：真报障 4 条必须放行 · 扩张**来自**共享字典 · 反向：无关指标不许被吞 · **前缀关键词必须仍匹配**——这条是我第一版词边界规则**被既有守卫当场抓红**后补的） |
| H2 「未获取到」其实是**五种**情形 | 界面把"未收录 / 不适用 / 边界不可区分 / 真缺口 / 环境限制"压成同一句 ⇒ 客户读成"系统坏了" | 新增 `NoApplicableData`（带**跨层标记**，因为异常经路由器聚合后**类型会丢、标记不会**）；`not_covered` 文案明确「该专题未收录本主体（**≠ 取值为 0**，非缺陷）」；`fedwatch` 那句"CME/FRED 不可达"是**错话**（FRED 可达）⇒ 从 `data_gaps` 拿掉并给 `alternative: fed:policy_range` | `tests/unit/test_gap_wording_and_news_limit.py`（9 条 + 3 条既有判据改契约；自证：把质押那处改回 `return []` ⇒ 行为判据立刻红） |
| H3 主线题材池"看不见" | 池级剔除 + 展示门槛两道门，报告里混成一句 | 解耦两个门槛（展示门槛 0.40 → **0.39**，池级门槛**保持 0.40 且写明"不许同值"**） | `tests/unit/test_mainline_alert_returns.py::test_display_gate_is_decoupled_from_pool_gate` |

---

## I. 公网链路与带宽（我主责体检，`CHG-0148`）

| 项 | 内容 |
|---|---|
| 症状 | 「打开单个概念的历史拥挤度，十几秒才出数据」 |
| 分环节实测 | SQLite 侧共 **≈13 ms**（`query_sector_meta` 5.9 ms、`query_sector_crowding` 3.3 ms、组装 0.2 ms），响应体 **424–502 KB** ⇒ 在隧道 **≈51 KB/s** 上 1.3–1.5 s，在已登记劣化档 **≈4.6 KB/s** 上 **14.1–16.1 s**（与用户说的"十几秒"逐字吻合） |
| 根因（三个，全是体积不是速度） | ① 浮点按完整 double 序列化（18–20 位小数共 2348 个，高熵尾数**压不动**）；② `SELECT *` 带出前端从不读的三列（`created_at`/`updated_at`/`id` = 明文的 **28.9%**，两个时间戳 1268 行逐字相同）；③ 接口没有客户端缓存 |
| 反直觉实测 | **只去字段几乎不降 gzip**（重复值本来就被压掉）；`871006.TI` 三档：现状 75,785 B · 只去三列 69,308 B · **再加精度取整 36,162 B（−52%）** ⇒ 真正决定体积的是**精度** |
| 修法 | 存储层新增 `slim=False` **opt-in** 参数（默认值即护栏：两个内部调用方依赖完整列与原始精度，直接改会**静默算错水位**）+ 接口显式 `slim=True` + 前端 SWR 缓存（LRU 8） |
| 判据 | `tests/integration/test_crowding_detail_payload_size.py`（12 条，阈值按最坏板块 1480 根标定、余量 1.5×：明文 ≤460 KB 实测 306,879 B · gzip ≤55 KB 实测 36,162 B · ★ **压比 ≤16%** 实测 11.8%，**这一条就是浮点精度的守卫**）+ `tests/unit/test_crowding_slim_projection.py`（11 条，含语法树断言"内部调用方不许传 slim=True"） |
| 链路体检（我新增 `scripts/check_public_link.py`） | 本机 5/5 中位 **3 ms**；主用 `hk.wujiaitool.cn` 5/5 中位 **759 ms**、带宽 **133.2 KB/s**（可用档 51 KB/s 的 2.6 倍）；备用 CF 首页 **403**（已登记的结构性形态） |
| 遗留 | 36 KB 在劣化档仍是 **7.1 秒** ⇒ 要再降必须改传输形态（分页/降采样/二进制列式），属产品变更，本轮没做 |

---

## J. 遗留未闭环（诚实清单，别当"已完成"）

| 项 | 现状 | 影响 |
|---|---|---|
| J1 worker 的 Windows 计划任务 | 未单独建（靠每分钟 `ensure` 兜住，已验证有效） | 开机后最多 1 分钟空窗 |
| J2 worker 崩溃现场 | 无内存/线程栈采集（后端那次有 `memory_snapshot()`） | 崩了只能看日志尾部 |
| J3 worker 侧 `loop_lag` | 没有该仪器 | "worker 卡住"（心跳仍新鲜）只能从 `runs.jsonl` 的 `duration_ms` 间接看出 |
| J4 dev 实例仍"全跑" | 有意：擅自改会静默改变用户本机行为 | dev 上重作业仍与在线请求共享循环 |
| J5 启动命令 11.2 s | 大头是多次 PowerShell 进程枚举（每次 ~1.1 s） | 再压需换掉命令行枚举（PID 文件 + 启动时间戳） |
| J6 其余 13 个启动/后台任务 | 实测只造成 ms 级空档（启动段最大 **45 ms**），未逐个错峰 | 非当前瓶颈 |
| J7 带必填参数的端点与 POST | 未纳入堵循环全量扫描 | 覆盖面缺口 |
| J8 「谁在飞」≠「谁在算」 | 在飞名单含长期 `await` 的任务（如每 25 秒心跳） | 仍需可复现重放定罪 |
| J9 同义词池的 251 处不对称 | 未收口 | 仍可能"采了白采" |
| J10 日志编码未统一 + `log_query.py` 未做 | `backend.log` 仍是混合编码、混多天 | 排查要靠逐行试探解码 |
| J11 7 个未登记的 `:all` 截面 | 已登记为成本决策项（+7 个日作业） | 待用户裁定 |
| J12 并发协作者的浮点改造收口 | `np.float32/16`、`np.int64` 类型漂移、`np.bool_` 三类已修；**前端缓存写入侧**是否同口径**未验证** | 可能前端仍写全精度 |
| J13 `test_safe_json_response` 之外 | 全量门禁剩余 2 条：1 条属并发会话浮点（已修）、1 条 `test_auth_service` 时间敏感抖动（单跑通过） | 抖动项未加固 |
| J14 代码生成/历史遗留 | `src/` 仍有未入库的政策性文件（`docs/` 审计文档、部分 `configs/*.yaml`、`web/src` 新面板） | 是否随仓库发布是政策判断，我不单方面决定 |

---

## K. 判据总表（机器强制，不靠"记得"）

| 判据文件 | 守什么 | 条数 |
|---|---|---|
| `tests/unit/test_scheduler_role_split.py` | 角色互斥且覆盖全集 · 名字必须在注册表 · worker 环境与 API 逐项相同 · `ensure`/`start`/`stop` 接线 · **`trigger()` 旁路必须被拦** · 三路存活证据 · 停止文件路径按目标环境求值 · `restart-pilot` 不许复用全量停止 | 12+ |
| `tests/unit/test_worker_liveness.py` | 心跳三态 + 损坏态 · 阈值 ≥3× 间隔 · **线程写 + 第一拍立刻** · 锁互斥且能说出持有者 · 锁字节在内容之外 · **内容写不进去锁仍算拿到** · 停止文件两条不变量 | 12 |
| `tests/integration/test_no_loop_blocking_endpoints.py` | 端点不得按住事件循环（`gap_max_ms`）+ **量法自证** | 2 |
| `tests/unit/test_api_no_loop_blocking.py` | AST：**全部路由文件**里 `async def` 不许直接调已知阻塞函数（含豁免标记 `# loop-blocking-ok`） | 7 |
| `tests/unit/test_inflight_registry.py` | 登记/注销成对 · 无在飞时为空串 · 行为判据（真请求触发登记）· 卡顿理由必须点名 · **依赖在 WS 上也要成立** | 7 |
| `tests/unit/test_startup_does_not_block_readiness.py` | 启动不许被重活挡住（AST×3 + 行为×2）· 后台任务必须具名 | 5 |
| `tests/unit/test_ws_scope_guard.py` | 非 WS 路径 ⇒ 404/1008（不是 500）· 静态挂载仍服务 HTTP · shim 合同 | 5 |
| `tests/unit/test_test_harness_isolation.py` | 测试不许对活着的 PID 发信号 · Settings 缓存必须逐用例清 | 3 |
| `tests/unit/test_cjk_quote_safety.py` | 我们自己写的每个 `.py` 必须能被 AST 解析（含自证） | 5 |
| `tests/unit/test_source_tree_is_tracked.py` | 生产代码/护栏不许未入库（只认 `??`，含自证与计数下界） | 3 |

**门禁读数**：全量 **6750 passed / 2 failed**（两条均已定位归属）· `prd_sync_check --ledger` ✅ · `--self-test` ✅。
