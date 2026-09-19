# 事件告警模块 500 故障：排查、方案选型与验证

> 项目：Moss-FinAgent-Research（多 Agent 投研工作台）
> 故障模块：事件告警（Event Alert，FR-10/FR-12）
> 故障日期：2026-09-18
> 文档性质：真实故障复盘，可直接作为面试项目讲述材料
> 涉及改动：`src/infrastructure/repositories/` 下 4 个文件 + 1 个新增回归测试

---

## 一、一分钟版本（面试开场用这个）

**现象**：事件告警页点"立即扫描"后，列表报 `Error: 请求失败(500): Internal Server Error`，显示"共 0 条 / 暂无符合条件的告警"。之前功能是好的，属于回归。

**定位**：从后端日志抓到精确 traceback，根因是 **SQLite 锁竞争**：
`GET /api/v1/alerts` 的读路径里包含一次写操作（"懒过期"`UPDATE fact_alerts`），
主库同时被做T采集/量化仓库/资金流等多条链写入，抢不到写锁直接抛
`sqlite3.OperationalError: database is locked`，把整个列表请求打成 500。

**修复**：三层措施 ——
① 读路径里的幂等清理"可失败"（降级而非报错）；
② 事件/告警仓储连接对齐项目统一的 `WAL + busy_timeout` 范式；
③ **等待时间按代价分级**（关键写 20s 排队 / 交互写 2s / 懒过期 0.4s 只试一次），避免长阻塞占死线程池。

**结果**：相关 8 个测试文件 70 passed；占锁场景下列表接口从"500"变为"0.5s 内正常返回"，且不再有线程池打满风险。

**一句话技术判断**：这是一个"**读接口里藏了写操作 + 多写者共享 SQLite + 连接参数不统一**"的复合问题；修复的关键不是"把超时调大"，而是要意识到*超时该按代价分级*——我第一版正是把超时调大，反而造成了比 500 更严重的全站阻塞，是靠压测把它否掉的。

---

## 二、系统背景与相关架构

| 维度 | 事实 |
|---|---|
| 后端 | FastAPI + uvicorn（单进程，`127.0.0.1:8100`），ASGI 单事件循环 |
| 主库 | SQLite `data/moss_finagent.db`，**约 1.3 GB**，`journal_mode=WAL` |
| 连接模型 | 原生 `sqlite3` 短连接 + `asyncio.to_thread` 卸载阻塞 IO（不上 ORM、不上连接池） |
| 并发写者 | 做T采集链路（路由回填）、量化数据仓库、资金流监控、事件告警扫描等**共享同一个库文件** |
| 告警链路 | 采集（多源，失败隔离）→ 标准化/跨源去重 → 事件幂等入库 → LLM 仅分析新事件（候选上限 30）→ 阈值引擎 → 24h 冷却 + 唯一约束 → WebSocket 广播 / 邮件 |
| 前端 | React（`web/src/`），`AlertsPanel.tsx` 在扫描期间每 1.5s 轮询 `/alerts/scan/latest` |
| 表结构 | `fact_events`（事件）、`fact_alerts`（告警），均带 `tenant_id`（多租户预留） |

**关键设计约束（决定了修复方案的空间）**：

1. 数据库是**共享的**：告警模块无法"独占"主库，也不能要求其它链路停写；
2. `fact_alerts` 有"懒过期"设计：读列表前把 `expire_time < now` 的告警置为 `expired`（未读红点到期自动消退）——这让**读路径天然带写**；
3. 服务是**单事件循环 + 默认线程池**：任何一次长时间阻塞的 DB 等待，都在消耗有限的线程，而不只是拖慢一个请求。

---

## 三、故障现象与影响面

- 页面表现：告警列表 `Error: 请求失败(500): Internal Server Error`，计数 `共 0 条`；
- 触发时机：**"立即扫描"之后**（扫描会一次性写入数十条事件）；
- 影响：用户完全看不到告警列表（详情面板也无数据），实时推送/铃铛未受影响；
- 复现性：直接 `GET /api/v1/alerts?limit=100` 又能返回 200（7 条），说明是**时序相关**而非稳定故障 —— 这一点是后续排查方向的关键线索。

---

## 四、排查过程（体现方法论的部分）

### 4.1 先建立事实，不猜

| 步骤 | 动作 | 结论 |
|---|---|---|
| 1 | 读路由代码 `src/api/routes/alerts.py` | 列表走 `runtime.event_repo.list_alerts(...)`，无特殊分支 |
| 2 | 直连运行中的后端做接口探测（`limit/type/level/status` 共 16 种参数组合） | 全部 200，仅非法 `limit=abc` 返回 422 → **不是参数/业务逻辑问题** |
| 3 | 从 `data/run/backend.log` 全文检索 ` 500 ` | 命中一条 `GET /api/v1/alerts?limit=100 500`，紧跟 `ERROR: Exception in ASGI application` |
| 4 | 打印该行上下文 90 行 | 拿到完整 traceback（见下） |
| 5 | 统计日志中 `database is locked` | **85 处**，且大量来自"路由回填DB失败…（不影响本次结果）" —— 证明主库长期存在多写者竞争 |

### 4.2 决定性证据：完整调用链

```
File "src/api/routes/alerts.py", line 168, in list_alerts
    alerts = await runtime.event_repo.list_alerts(...)
File "src/infrastructure/repositories/alert_sqlite_repo.py", line 53, in list_alerts
    return await asyncio.to_thread(self._list_alerts_sync, ...)
File "src/infrastructure/repositories/alert_sqlite_repo.py", line 66, in _list_alerts_sync
    self._expire_due(conn, now_iso(), tenant_id)          # ← 读路径里的写
File "src/infrastructure/repositories/alert_sqlite_repo.py", line 41, in _expire_due
    conn.execute("UPDATE fact_alerts SET status = 'expired' ...")
sqlite3.OperationalError: database is locked
```

日志时间线还给出了因果链：

```
09:19:32  POST /api/v1/alerts/scan            202 Accepted
          ...（扫描批量写入 64 条事件、113 条候选）
09:19:3x  路由回填DB失败（不影响本次结果）: stock_close:301086 -> database is locked
09:19:3x  GET /api/v1/alerts?limit=100        500 Internal Server Error   ← 用户看到的报错
```

### 4.3 用数据确认"连接参数不统一"

对全仓库检索 `sqlite3.connect`，逐个核对连接参数：

| 仓储 | 连接参数 | 结论 |
|---|---|---|
| `sector_crowding/db.py` | `timeout=30.0` + `WAL` + `busy_timeout=30000` | 规范 |
| `quant/quant_select_repo.py` | `timeout=15.0` + `WAL` + `busy_timeout=15000` | 规范 |
| `fund_flow_sqlite_repo.py` | `timeout=15.0` + `WAL` + `busy_timeout=15000` | 规范 |
| `auction_select/db.py` | `timeout=30.0` + `WAL` + `busy_timeout=30000` | 规范 |
| **`event_sqlite_base.py`**（事件/告警） | **裸 `sqlite3.connect()`，无 PRAGMA** | **例外** |
| **`macro_repo.py`**（数据点） | **裸 `sqlite3.connect()`，无 PRAGMA** | **例外** |

实测：Python 3.12 裸连接的默认 `busy_timeout` 只有 **5000ms**，而项目规范是 15~30s。
**故障不是"某个模块写错了"，而是"两条链路游离在项目既定范式之外"。**

---

## 五、根因分析（三个因素叠加）

```
        ┌──────────────────────────────────────────────┐
        │ 因素 A：主库被多条链并发写（做T/量化/资金流）  │
        │   扫描一次再追加数十条事件写入                 │
        └───────────────────┬──────────────────────────┘
                            │ 产生持锁窗口
        ┌───────────────────▼──────────────────────────┐
        │ 因素 B：读接口里藏了一次写（懒过期 UPDATE）    │
        │   抢不到锁 → 直接把整个 GET 打成 500          │
        └───────────────────┬──────────────────────────┘
                            │ 放大了影响面
        ┌───────────────────▼──────────────────────────┐
        │ 因素 C：连接参数游离于项目范式（默认 5s 等待） │
        │   事件/告警、宏观数据点两个仓储未设 WAL/超时   │
        └──────────────────────────────────────────────┘
```

**为什么"之前能用、现在不行"**：系统里并发写者变多了（二期加入量化仓库/盘中T+0/资金流监控等），共享主库的写竞争显著上升，原本"偶尔撞上"的窗口变成了"扫描时必撞"。

**为什么本地/单发请求复现不了**：`database is locked` 是时序竞态，只有在持锁窗口内并发才有；这也解释了"直接请求又能 200"。

---

## 六、方案选型（核心：三个候选方案的取舍）

### 候选方案 A：给读路径加长超时（"把它等过去"）

- 做法：连接 `timeout=20s`，让 `UPDATE` 排队等锁。
- 优点：改动最小，几乎必不报错。
- 缺点：**把错误变成了延迟**；更严重的是——服务是单事件循环 + `to_thread` 默认线程池，一个请求阻塞 20s 就占死一个线程。
- **实测否决**：占锁 12s 时连续 4 次列表请求**全部超时**（客户端 30s 超时），前端整页卡死。比 500 更糟。→ **放弃**。

### 候选方案 B：让懒过期彻底异步化（后台任务/定时器）

- 做法：读路径不写，改由后台任务定期把到期告警置 `expired`。
- 优点：读路径纯净，语义最干净。
- 缺点：需要引入后台任务生命周期（启动/取消/幂等），与现有"读时懒迁移"的既定设计（`FR-8/AC-6`）冲突；改动面从 2 个仓储扩散到应用生命周期，面试里也难讲清收益边界。
- 结论：**记录为后续演进方向，本次不做**（先修故障、控制爆炸半径）。

### 候选方案 C（采纳）：可降级 + 分范式统一 + 超时按代价分级

1. **清理动作可失败**：懒过期抢不到写锁就跳过并返回 `False`（不抛），列表照常返回。
   - 依据：过期是"懒迁移 + 幂等"的清理，**不是读结果的一部分**；本次跳过，下次读/写自然会补上。
2. **仓储连接统一到项目范式**：抽出 `connect_sqlite()`（WAL + busy_timeout + `synchronous=NORMAL`），事件/告警仓储与数据点仓储接入。
3. **超时按代价分级**（关键设计决策）：

| 场景 | 连接等待 | 重试 | 理由 |
|---|---|---|---|
| 关键写：事件入库 / 告警入库 | 20s | 3 次退避 | 丢一条告警 = 丢一次推送 + 一封邮件，**值得排队** |
| 交互式读：列表 / 详情 / 未读数 | 2s | — | 读在 WAL 下几乎不阻塞；不该为写锁排 20s |
| 交互式写：标记已读 / 全部已读 | 2s | — | 宁可快速失败让前端重试，也不钉住请求 |
| 读路径顺带清理：懒过期 | **0.4s** | **只试一次** | 必须远小于前端 1.5s 轮询间隔；失败无副作用 |
| 建表/迁移 DDL（幂等） | 2s | 3 次退避 | 幂等 DDL，不该让首个读请求长时间排队 |

4. **写路径加锁重试**：`retry_on_locked()` 仅针对锁竞争错误重试（其它 SQLite 错误立即上抛），避免把真实故障掩盖成"重试中"。

**选型原则（可复用的一句话）**：
> 在共享资源竞争的修复里，"谁值得等、谁必须快速失败"要按业务代价区分；
> 对所有路径统一调大超时是最危险的做法 —— 它会把局部错误升级为全局阻塞。

---

## 七、最终实现

### 7.1 `event_sqlite_base.py`：统一连接 + 两个工具函数

```python
CONNECT_TIMEOUT_S = 20.0      # 关键写：值得排队
BUSY_TIMEOUT_MS = 20000
FAST_TIMEOUT_S = 2.0          # 读/交互：秒级失败即降级
FAST_BUSY_TIMEOUT_MS = 2000
EXPIRE_TIMEOUT_S = 0.4        # 懒过期单次抢锁上限（< 前端 1.5s 轮询）

def connect_sqlite(db_path, timeout_s=None, busy_timeout_ms=None):
    conn = sqlite3.connect(db_path, timeout=CONNECT_TIMEOUT_S if timeout_s is None else timeout_s)
    conn.row_factory = sqlite3.Row
    with suppress(sqlite3.Error):        # journal_mode 需要写锁，失败不阻断连接
        conn.execute(f"PRAGMA busy_timeout={int(busy)}")
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    return conn

def is_locked_error(exc) -> bool:        # 只认锁竞争，不吞其它错误
    return isinstance(exc, sqlite3.OperationalError) and (
        "locked" in str(exc).lower() or "busy" in str(exc).lower())

def retry_on_locked(fn, attempts=3, delay_s=0.2):   # 写路径兜底重试
    ...
```

> 实现细节：等待参数在**调用点读取模块常量**（而非函数默认参数），这样测试可以 monkeypatch 成 0，让"抢不到锁"立刻发生，无需真等 20s。

### 7.2 `alert_sqlite_repo.py`：清理可降级 + 独立短超时连接

```python
def _expire_due(self, tenant_id: str, now_text: str) -> bool:
    """懒过期：读路径里唯一的一次写。
    1) 抢不到写锁就跳过（返回 False），列表照常返回；
    2) 用独立的 0.4s 短超时连接且只试一次，绝不复用读连接的长排队参数。
    """
    try:
        with connect_sqlite(self._db_path, timeout_s=EXPIRE_TIMEOUT_S,
                            busy_timeout_ms=int(EXPIRE_TIMEOUT_S * 1000)) as conn:
            conn.execute("UPDATE fact_alerts SET status = 'expired' "
                         "WHERE tenant_id = ? AND status IN ('active','read') "
                         "AND expire_time != '' AND expire_time < ?",
                         (tenant_id, now_text))
        return True
    except Exception as exc:
        if is_locked_error(exc):
            logger.warning("告警懒过期跳过（主库写锁繁忙，不影响本次读取）: %s", str(exc)[:120])
            return False
        raise
```

配套：`EventSqliteStore._connect_fast()`（2s 档）；`list_alerts / get_alert / count_unread / mark_read / mark_all_read` 全部改走短超时连接；
`_upsert_alerts_sync` / `_upsert_events_sync` / `_mark_analyzed_sync` 走 20s 档 + `retry_on_locked`。

### 7.3 改动清单

| 文件 | 改动 |
|---|---|
| `src/infrastructure/repositories/event_sqlite_base.py` | 新增 `connect_sqlite` / `is_locked_error` / `retry_on_locked`；分级超时常量；`_schema_sync` 拆分为带重试的幂等 DDL |
| `src/infrastructure/repositories/alert_sqlite_repo.py` | 懒过期可降级 + 独立短超时连接；读/交互走 fast 连接；写路径加锁重试 |
| `src/infrastructure/repositories/event_sqlite_repo.py` | 事件入库、标记已分析接入锁重试 |
| `src/infrastructure/repositories/macro_repo.py` | 裸连接改为统一 `connect_sqlite` |
| `tests/unit/test_alert_sqlite_lock.py` | **新增**：8 条锁竞争回归测试 |

---

## 八、验证（面试重点：我怎么证明它真的修好了）

### 8.1 回归测试：主动"制造"故障条件

新增 `tests/unit/test_alert_sqlite_lock.py`，用另一条连接 `BEGIN IMMEDIATE` **真正占住写锁**，再调仓储：

| 用例 | 断言 |
|---|---|
| `test_reader_connection_waits_for_main_db_lock` | 连接带 `busy_timeout=20000`、`journal_mode=wal` |
| `test_interactive_connection_uses_short_wait` | 交互连接必须是短等待（防止有人改回长排队） |
| `test_list_alerts_survives_locked_main_db` | 占锁时列表照常返回，告警状态保持 `active`（过期降级为"下次再说"） |
| `test_list_alerts_latency_is_bounded_when_db_is_busy` | 占锁 1s 时列表耗时 **< 1s**（守住"读路径不许长阻塞"） |
| `test_expire_due_reports_deferred_cleanup` | 抢不到锁返回 `False` 而非抛异常 |
| `test_upsert_alerts_retries_until_lock_released` | 占锁者释放后，同一次 upsert 仍成功入库（不丢告警） |
| `test_retry_on_locked_retries_lock_but_not_other_errors` | 只对锁错误重试；`no such table` 立即上抛 |

### 8.2 反向验证：先证明测试"抓得住"这个 bug

把 `_expire_due` 临时还原成旧实现，跑同一组用例：

```
E  sqlite3.OperationalError: database is locked
src\infrastructure\repositories\alert_sqlite_repo.py:50: OperationalError
2 failed, 4 passed
```

报错与生产 traceback 完全一致 → 用例确实锁住了这个回归，而不是"写了几个必过的测试"。

### 8.3 分层实测数据

| 场景 | 修复前 | 修复后 |
|---|---|---|
| 主库被占时 `GET /api/v1/alerts` | **500**（用户可见报错） | **200，正常返回 7 条** |
| 仓储层单次列表耗时（占锁） | — | **0.52s**（全部是懒过期那一次抢锁，SELECT 不受 WAL 写事务影响） |
| 占锁 12s 期间的请求 | 连续 4 次全部超时（第一版误改造成） | 秒级返回，线程池不被占死 |

### 8.4 端到端

- 通过完整 ASGI 栈（真实 app + 独立 DB 副本）复现并验证；
- 运行中的后端在重启后，日志中 `/api/v1/alerts?limit=100` **连续多次 200，无新 traceback**；
- 相关 8 个测试文件：**70 passed**；`ruff` 全绿。

---

## 九、这次踩的坑（面试最加分的一段：我的判断被数据推翻）

**第一版修复其实是错的。**

我先做了"给读路径加 20s 超时"（候选方案 A），本地单发请求看起来完美：不再 500。
但我没有停在"单发通过"，而是**占住写锁 + 连续请求**做压测：

```
[lock] 已占写锁 12s
[probe 1] HTTP 200（等待 14.18s）
[probe 2] ERROR TimeoutError（>30s 客户端超时）
[probe 3] ERROR TimeoutError
[probe 4] ERROR TimeoutError
```

结论：把"读路径顺带的清理写"设成 20s 排队，会让**每个请求占死一个线程**；单事件循环 + 默认线程池下，主库繁忙时线程池被打满，连健康检查都超时 —— **局部错误被升级为全局阻塞**。

于是才有最终的三档设计：**关键写排队 / 交互写秒级 / 懒过期 0.4s 只试一次**。
这件事的通用教训是：

> 修并发问题的第一反应不该是"把超时调大"，而要问：
> **这个等待值得吗？谁在为它付费（哪个线程/哪条链路）？失败能不能降级？**

---

## 十、可复用要点 & 面试追问准备

### 10.1 六个可直接复用的工程结论

1. **读接口里不要藏写操作**；如果必须有（懒迁移/懒清理），要设计成"可失败 + 幂等"。
2. **共享 SQLite 的并发范式要统一**：`WAL + busy_timeout + 显式 timeout`，并且**新模块接入时对齐既有范式**（本次故障就是两个仓储游离在范式之外）。
3. **超时按业务代价分级**，不做"一刀切调大"。
4. **阻塞等待会占用线程这一稀缺资源**：单事件循环 + `to_thread` 的架构下，长等待的破坏力是全局的。
5. **日志里的"次要错误"（`路由回填DB失败（不影响本次结果）`）是重要线索**：它说明共享资源已长期紧张，只是还没打到用户可见的路径上。
6. **回归测试要能"抓住"原 bug**：先用旧实现把测试跑红，再上修复跑绿（红-绿证据链）。

### 10.2 高频追问与回答要点

**Q：为什么不用 PostgreSQL / 换掉 SQLite？**
A：项目定位是"SQLite 零依赖、单机可跑"（`DATA_BACKEND=sqlite`，Postgres/Redis 可选）。1.3 GB 库 + 多写者在这个定位下是合理的，且本次故障不是 SQLite 的能力问题，而是**连接参数与读路径写操作**的问题。换库是"用架构变更掩盖工程问题"，成本与风险都更高；真要演进，先做写入收敛（单写者/队列），再谈换库。

**Q：为什么不干脆把懒过期改成后台定时任务？**
A：这是方案 B，属于更彻底的设计，但会引入后台任务生命周期（启动/取消/幂等），并把改动面从仓储扩散到应用装配；在故障止血阶段，优先"最小爆炸半径 + 可验证"。我把它作为后续演进方向记录了。

**Q：`retry_on_locked` 为什么不重试所有异常？**
A：sqlite 错误语义不同。锁竞争是**瞬时、可重试**；`no such table`/schema 错误是**确定性失败**，重试只会拖延暴露问题的时间。所以只对 `locked`/`busy` 重试。

**Q：为什么不给 SQLite 上连接池？**
A：Sqlite 短连接成本低，且连接不跨线程共享（`sqlite3` 默认 `check_same_thread=True`），与 `asyncio.to_thread` 的"每操作一连接"模型匹配；引入池要额外处理 WAL/事务边界与跨线程归还，收益不明确。本次问题靠参数与语义修复即可解决。

**Q：如何证明"不是偶发、而是真的修好了"？**
A：三条证据：① 确定性占锁回归测试（红→绿）；② 反向验证（还原旧实现即变红，报错与生产一致）；③ 运行实例日志中故障接口连续 200、无新 traceback。

**Q：这个修复对性能有影响吗？**
A：读路径额外成本的上界是懒过期那次抢锁（实测占锁时 0.52s，不占锁时几乎为 0）；SELECT 本身不受 WAL 写事务阻塞。代价换来的是"主库繁忙时列表可用"，以及不再有线程池被打满的风险。

### 10.3 一句话收尾（面试结尾可用）

> 这个故障的价值不在"改了几行代码"，而在于它暴露了三件事：
> 读路径不该有隐藏写、共享资源的并发范式必须统一、超时必须按代价分级。
> 我第一版用"加大超时"把它修成了更严重的全局阻塞，是靠压测把自己否掉的 ——
> 这让我后来形成了习惯：**先定义失败语义，再谈等待时长**。

---

## 附录 A：故障时的关键日志证据

```
INFO:  127.0.0.1:62694 - "POST /api/v1/alerts/scan HTTP/1.1" 202 Accepted
路由回填DB失败（不影响本次结果）: stock_close:301086 -> database is locked
INFO:  127.0.0.1:62019 - "GET /api/v1/alerts?limit=100 HTTP/1.1" 500 Internal Server Error
ERROR: Exception in ASGI application
Traceback (most recent call last):
  ...
  File "src/api/routes/alerts.py", line 168, in list_alerts
    alerts = await runtime.event_repo.list_alerts(
  File "src/infrastructure/repositories/alert_sqlite_repo.py", line 53, in list_alerts
    return await asyncio.to_thread(
  File "src/infrastructure/repositories/alert_sqlite_repo.py", line 66, in _list_alerts_sync
    self._expire_due(conn, now_iso(), tenant_id)
  File "src/infrastructure/repositories/alert_sqlite_repo.py", line 41, in _expire_due
    conn.execute(
sqlite3.OperationalError: database is locked
```

（同一份日志中 `database is locked` 共 85 处，多数来自做T采集的"路由回填"路径。）

## 附录 B：术语与文件索引

| 术语/文件 | 说明 |
|---|---|
| 懒过期（lazy expire） | 读列表前把 `expire_time < now` 的 active/read 告警置 `expired`，实现未读红点自动消退 |
| WAL | SQLite 预写日志模式，允许"一写多读"并发 |
| `busy_timeout` | SQLite 遇到锁时的等待毫秒数（Python 裸连接默认仅 5000ms） |
| `asyncio.to_thread` | 把阻塞 IO 卸载到默认线程池，避免阻塞事件循环 |
| `src/api/routes/alerts.py` | 告警 REST/WS 路由（列表、详情、已读、扫描、手工导入、阈值配置） |
| `src/infrastructure/repositories/alert_sqlite_repo.py` | 告警表 CRUD 与过期状态机（本次修复核心） |
| `src/infrastructure/repositories/event_sqlite_base.py` | 事件/告警仓储共享件（连接、schema、行映射；本次新增连接与重试工具） |
| `tests/unit/test_alert_sqlite_lock.py` | 锁竞争回归测试（本次新增） |
