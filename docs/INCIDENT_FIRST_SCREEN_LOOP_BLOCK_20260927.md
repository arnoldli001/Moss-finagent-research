# 首屏卡死事故：一个体检接口把整个后端按住（2026-09-27）

> 用户报障原话：
> 「这个项目的前端 https://hk.wujiaitool.cn/ 首次登录进去，**事件告警**有，
> 而**策略回测、主线挖掘、资金流监控**都是要等很久都不出来？已经优化过几轮，
> 还是没解决问题」，并附带间歇性提示：
> 「**后端服务当前不可达**。刚才那些「无法连接」的提示就是这么来的 ……」

**结论先说**：那三个面板都没做错什么，真凶是「策略回测」首屏顺手拉的一个
**体检接口** —— `GET /api/v1/quant/data-status`。它把**同步阻塞的活**
（扫分区目录 + 逐表 `MIN/MAX(日期)`）写在 `async def` 里，跑在**事件循环线程**上；
单进程 uvicorn 只有一个循环，于是首屏那十几个并发请求**全部排队**，
连 0 I/O 的 `/api/v1/health/live` 都被拖到 **12.75 秒**（前端判死线 **3 秒**）
→ 用户看到「等很久都不出来」+ 假报「后端不可达」。

---

## 一、证据（全部本机/线上实测，不靠读代码猜）

### 1.1 单个接口看上去全都是好的 —— 这就是前几轮优化没能解决的原因

一条条 curl 打（顺序请求），全部 ≤2 秒：

| 接口 | 顺序请求耗时 |
|---|---|
| `/api/v1/health/live` | 0.02s（本机）/ 0.69s（公网） |
| `/api/v1/mainline/snapshot` | 1.33s |
| `/api/v1/fundflow/snapshot` | 1.55s |
| `/api/v1/quant/data-status` | **0.02s**（缓存命中时） |

**问题只在并发时出现**：把浏览器首屏真实发出的那 14 个请求**同时**打出去，
每一个都要 **40~52 秒** —— 包括 `/api/v1/sector_rotation/history`（0.1 KB）：

```
主线·告警收益(龙头)   52.80s  HTTP 200
资金流·快照           44.25s  HTTP 200
主线·期货             41.28s  HTTP 200
…
板块轮动·历史         40.53s  HTTP 200     ← 0.1KB 的响应也等了 40 秒
合计墙钟 52.80s；单请求中位 40.53s
```

「所有请求几乎同一时刻返回」是**共享阻塞点**的典型特征。

### 1.2 直接把栈打出来（`py-spy`，请求风暴期间连打 4 次）

```
Thread 29324 (active): "MainThread"
    stat (pathlib.py:840)
    is_file (pathlib.py:892)
    keys (src\quant\dataset_store.py:127)
    coverage (src\quant\dataset_store.py:186)
    data_status (src\api\routes\quant.py:92)      ← 路由（async）里直接干同步活
    …
Thread 29324 (active): "MainThread"
    stats (src\quant\warehouse.py:833)
    warehouse_status (src\quant\warehouse.py:1454)
    data_status (src\api\routes\quant.py:110)     ← 同一请求的下一段
```

**四次采样，事件循环线程每次都停在这条链上** —— 它不是"忙"，是被按住。

### 1.3 这 7~13 秒花在哪（仓库现状）

```
dialect = sqlite    表数 = 10    总行数 ≈ 1.0 亿
    daily        15421112 行      20060104 ~ 20260924
    daily_basic  15420943 行      …
    adj_factor   15977992 行      …
    moneyflow    14059504 行      …
    …
stats() 单独跑 7.18s；公网首屏实测 13.09s
```

每张表一条 `SELECT MAX(rowid), MIN(trade_date), MAX(trade_date)` ——
`MAX(rowid)` 是 O(1)，但**日期列没有索引**，`MIN/MAX` 就是**全表扫描**，
15M 行 × 10 张表 ≈ 7 秒，全在事件循环上。

### 1.4 假报「后端服务当前不可达」是怎么来的

前端 `/health/live` 探针是 **3 秒**超时（`web/src/api.ts` 的 `pingServer`）。
风暴期间实测：

| | 修复前 | 修复后 |
|---|---|---|
| 风暴中 `/health/live` 最慢一次 | **12 750 ms** | **578 ms** |
| 超过 3 秒判死线 | 1/22 次 | **0/125 次** |
| 首屏风暴墙钟（冷缓存） | 48.6s | 27.2s |
| 首屏风暴墙钟（热缓存） | 40.7s | **0.27s** |

---

## 二、修法（`src/api/routes/quant.py`）

1. **整段体检挪出事件循环**：拆出**同步**函数 `_collect_data_status()`，
   路由改成 `await asyncio.to_thread(_collect_data_status, …)` ——
   同步签名让"误在循环里调用"一眼可见；
2. **结果缓存 300 秒**：数据条反映"数据同步到哪一天"，一天才变一次；
   前端每次打开面板都会拉它，缓存把"每人每次开面板扫一遍"降成"每 5 分钟最多一次"；
3. **启动预热**（`api/main.py` 的 `_warm_quant_data_status`）：启动 12 秒后
   后台算一遍，让**第一个用户**也走缓存。同一天修掉的坑：第一次写预热时把
   `root` 写死成 `"data/quant"`，而路由默认是 `DEFAULT_ROOT = "data/quant/tushare"`
   → **缓存键对不上，预热静默失效**（第一个用户照样等 11 秒，日志里毫无异常）。
   现在两处默认值都引用 `DEFAULT_ROOT`，并有守卫测试盯着。
4. 同一文件 `/ic` 里的 `store.keys()`（扫目录）一并挪进线程。

### 守卫（防复发）

`tests/unit/test_api_no_loop_blocking.py` —— 用 AST 断言钉住不变式
（只看路由**自己的语句**，丢进 `to_thread` 的嵌套函数不算，docstring/注释不看）：

* `data_status` 路由体里不许出现 `iterdir/glob/keys/coverage/warehouse_status`；
* 它必须用 `asyncio.to_thread` 调 `_collect_data_status`；
* `_collect_data_status` 必须是**同步** `def`；
* 必须带 TTL 缓存，且 TTL ≥ 60 秒；
* `warm_data_status` 的 `root` 默认值必须引用 `DEFAULT_ROOT`（防缓存键漂移）；
* 全局：任何 `async def` 路由都不许直接调已知阻塞函数。

---

## 三、顺带修掉的第二件事：恢复窗口 5 分钟 → 1 分钟

排查时发现线上 **502**：后端进程在 16:06 静默消失（`backend.log` 最后一行还是
`200 OK`），而 `MossPilotWatchdog` 计划任务的间隔是 **5 分钟**
（脚本自己的说明写的是"配计划任务**每分钟**跑一次"）——
于是「进程消失 → 用户看到 502」这段窗口最长 5 分钟，正是"间歇性不可达"的另一半。

已把 `MossPilotWatchdog` 的重复间隔改成 **PT1M**（`ensure` 幂等、健康时零副作用、
自带单实例锁，不存在副作用）。**进程为什么消失仍未定论**（`backend_incidents.jsonl`
记了 4 次，内存/提交量都在 72~74%，无崩溃事件、无关机记录，像被强杀）——
下次再犯时优先看那份流水的 `mem_*` 与 `last_activity`，并把间隔视为"最长不可用时间"。

---

## 四、复跑与验收

```powershell
# ① 首屏实测（真浏览器 + 真链路；冷启动最接近用户首次登录）
.venv\Scripts\python.exe manage.py stop  --env pilot --port 8110
.venv\Scripts\python.exe manage.py ensure --env pilot --port 8110
C:\veighna_studio\python.exe scripts\_verify_first_login_load.py https://hk.wujiaitool.cn

# ② 阻塞守卫
.venv\Scripts\python.exe -m pytest tests/unit/test_api_no_loop_blocking.py -q
```

**修复后实测**（公网、真浏览器、冷启动）：

| 页签 | 点开后最慢请求 | 页面提示 |
|---|---|---|
| 首屏自动 14 个请求 | 0.83s，全部 200 | 干净 |
| 策略回测 | 6.43s（`/intraday/snapshot`；`/quant/data-status` 已降到 **0.48s**） | 干净 |
| 主线挖掘 | 1.41s | 干净 |
| 资金流监控 | 1.27s | 干净 |
| 事件告警 | 2.10s | 干净 |

---

## 五、这次的教训（写给下一个改后端的人）

1. **`async def` 不是"并发安全"的同义词** —— 它只说明"跑在事件循环上"。
   循环上任何一次同步 I/O，都是**全站**的排队点；单进程 uvicorn 只有一条循环。
2. **性能问题要看并发，不看单请求**。前几轮优化都只量了单接口耗时，
   那张表一直好看；而用户遇到的是"首屏 14 个请求同时打"的形态。
   所以本仓库现在的验收脚本 `scripts/_verify_first_login_load.py`
   量的是**并发下每个请求各自花了多久 + 页面上有没有出现"不可达"字样**。
3. **API 越大越容易藏这种活**：`data-status` 这种"顺手体检"的接口最危险 ——
   它看起来只是个状态展示，实际在扫 1 亿行。
4. **预热要断言缓存键一致**：写死的默认值会与路由漂移，而且**静默失效**。
5. **"后端不可达"的提示要先怀疑自己**：这次后端全程活着，只是被一个请求按住了。
