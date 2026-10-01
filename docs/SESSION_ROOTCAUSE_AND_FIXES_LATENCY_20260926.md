# 本会话问题清单 · 根因 · 解决方案（**公网性能 / 静默丢数据** 专题）

> 会话范围：**投资日历"刷不出来数据"** + **行业轮动日报 / 板块拥挤度 页签来回切换延迟 1~2 秒**
> 会话日期：2026-09-26 20:30 ~ 2026-09-27
> 姊妹文档（主题不同，勿混读）：
> - `SESSION_ROOTCAUSE_AND_FIXES_INTEL_20260927.md`（情报中心数据正确性）
> - `SESSION_ROOTCAUSE_AND_FIXES_SCOPE_RELEASE_20260927.md`（范围收敛与发布）
>
> **本文只收本会话真实发生、且我能给出命令级证据的问题。** 每条格式统一：
> **症状 → 根因 → 修法 → 证据**。凡属推断的地方都显式标注"**推断**"。
>
> 证据来源：`data/run/backend.log`（45.5 万行）、`data/pilot/moss_pilot.db`（会话表）、
> `data/cache/intel/*.json`（慢聚合缓存）、curl 实测公网/本机时延。

---

## 0. 一句话结论

**这两个报障都不是"后端算错了"或"数据没了"，而是"数据算对了，但没能活着到达用户眼前"。**

| # | 报障 | 真病灶落在哪一层 |
|---|---|---|
| P1 | 投资日历刷不出来 | **前端**——6 秒硬超时把已经拿到的日历数据**静默丢弃**（后端与缓存均正常） |
| P2 | 轮动/拥挤度切页签延迟 1~2 秒 | **前端结构**——三元链导致每次切页签都**卸载重建 + 重新取数** |

两者的共同结构性背景是**第三条**，它才是真正的"放大器"：

| # | 结构性背景 | 数字 |
|---|---|---|
| P0 | 公网链路（Cloudflare 隧道）的固定开销与抖动 | 一次**空接口** `/health/live` 都要 **1.19~5.05 s**；抖动时 15.8 s；还会直接回 **502** |

**推论（本会话最重要的方法论产出）**：
**"本机 40 ms" 与 "公网 2 s" 之间没有关系。** 任何以"本机很快"为依据定下的超时值、
重试策略、往返次数预算，在公网上都是**未经验证的假设**。

---

## 1. P1 —— 投资日历"刷不出来数据"

### 1.1 症状

用户口径：**"前端的投资日历 怎么刷不出来数据了"**。
界面上**没有红色报错**、也**没有转圈**，就一行兜底文案：

```
暂无日程数据
```

### 1.2 排查过程（含被推翻的假设，见 §5）

关键转折点是**放弃猜前端，改为"把浏览器要发的那条请求原样发出去"**。
用 `data/pilot/moss_pilot.db` 里一条**真实有效会话**的 `moss_sid` Cookie，
分别打本机 8110 与公网域名，得到：

| 路径 | 状态 | 耗时 | 体积 |
|---|---|---|---|
| 本机 `/api/v1/intel/calendar?horizon_days=45` | 200 | **40~116 ms** | 58 KB (gzip) |
| 公网 同一条 | 200 | **2.0 / 5.0 / 18.6 / 61.5 s** | 58 KB (gzip) |
| 公网 同一条（未带 `Accept-Encoding`） | 200 | 34.6 s（TTFB 3.9 s） | **675 KB（未压缩）** |
| 公网 `/api/v1/health/live`（**什么都不做**） | 200 | **1.19 / 1.45 / 4.51 / 5.05 s** | — |
| 公网 同上，抖动时 | **502** | 8.56 s | — |

**后端与缓存是干净的**，同一个缓存文件用领域层直接读，数据完整：

```
cache hit: True   age_hours: 6.03   stale: True
events: 2356   degraded: False   gaps: []
date range: 2026-09-27 → 2026-11-10   (33 个未来日期)
kinds: {'macro': 7, 'unlock': 27, 'trade_day': 2, 'earnings': 2320}
```

### 1.3 根因（三段，缺一不可）

**① 前端超时是 6 秒 —— 一个按"本机/快网"标定的值**

`web/src/components/intel/IntelPanel.tsx:59`

```ts
const LOAD_TIMEOUT_MS = 6000;
```

第 254 行 `window.setTimeout(() => ac.abort(), LOAD_TIMEOUT_MS)`。
源码注释自己写着设定依据：*"正常情况（本机 30 ms、公网热 1~3 秒）远到不了它"* ——
而实测公网 TTFB 已达 **6.8 s**，该前提**不成立**。

**② 超时后日历数据被"静默丢弃"——这是用户看不到任何错误的原因**

`web/src/components/intel/IntelPanel.tsx:268-279`

```ts
if (r.calendar) setCal(r.calendar);      // ← abort 后根本不会执行
...
const aborted = e instanceof DOMException && e.name === "AbortError";
if (!aborted) {
  setErrFeed(errText(e));
  setErrCal(errText(e));
} else if (!cached) {                     // ← 有 feed 缓存时，这里也不成立
  setErrFeed("请求超时（链路较慢）。可稍后点刷新重试。");
}
```

于是落进第 493-494 行的兜底分支：

```tsx
) : (
  <LoadingBlock label="暂无日程数据" />
)
```

**③ 为什么"情报流看着正常、只有日历空"**

这是本问题最容易被误判的地方，也是它能长期潜伏的原因：

| | 情报流 | 投资日历 |
|---|---|---|
| 本地缓存 | **有**（`writeIntelFeed` → localStorage） | **无** |
| abort 后 | 照旧显示手里那份旧数据（用户觉得"还行"） | `cal` 恒为 `undefined` → 显示"暂无日程数据" |

**同一个 abort 事件，一边被缓存遮住了，另一边裸奔。** 用户看到的"只有日历坏了"，
其实是一次全局失败被缓存掩盖了一半。

### 1.4 修法

| 优先级 | 改动 | 位置 | 判据 |
|---|---|---|---|
| P0 | `LOAD_TIMEOUT_MS` `6000` → `20000` | `IntelPanel.tsx:59` | 4/5 次公网实测 ≤ 18.6 s，20 s 可覆盖 |
| P0 | abort 后若 `feed` 到位而 `cal` 为空，**补发一次 `/intel/calendar`** | `IntelPanel.tsx` catch 分支 | 58 KB 远轻于 bootstrap 的 943 KB |
| P1 | `INTEL_SLOW_CACHE_HOURS` `6` → `24` | `.env`（代码默认见 `src/core/config.py:367`） | 重建一次实测 **13.6 s**，撞上抖动窗口即超时 |
| P2 | 瘦身 payload | `src/domain/intel/calendar.py` | 2356 条中 2320 条是预约披露；单条解禁事件携带全部 40 只票的 `market_cap`/`pct_of_float`/`share_type` |

### 1.5 会话内的处置

缓存已过期（`age_hours 6.16 > TTL 6.0`），当场强制重建并验证：

```
intel_slow_cache_hours = 6.0     intel_prewarm_enabled = True
prewarm_slow(force=True) took 13.6s -> {'rebuilt': ['calendar_45', 'heat'], 'failed': []}
AFTER calendar_45: age_h=0.00 stale=False events=2356
```

### 1.6 证据（可复现）

```powershell
# 本机 vs 公网，同一会话 Cookie、同一路径、都带 gzip
curl.exe -s -o NUL -w "%{http_code} %{time_total} %{size_download}" `
  -H "Accept-Encoding: gzip" -H "Cookie: moss_sid=<sid>" `
  "http://127.0.0.1:8110/api/v1/intel/calendar?horizon_days=45"

curl.exe -s -o NUL -w "%{http_code} %{time_total} %{size_download}" `
  -H "Accept-Encoding: gzip" -H "Cookie: moss_sid=<sid>" `
  "https://moss.wujiaitool.cn/api/v1/intel/calendar?horizon_days=45"
```

---

## 2. P2 —— 行业轮动日报 / 板块拥挤度 切页签延迟 1~2 秒

### 2.1 症状

用户口径：**"行业轮动日报、板块拥挤度 这两个界面来回切换，每次加载内容就延迟 1-2 秒"**
关键词是**"每次"**与**"来回"** —— 这不是"第一次慢"，而是**每次切都慢**。

### 2.2 根因

**① 切页签 = 卸载 + 重新挂载：`FundFlowPanel` 用的是三元链**

**修复前**（`web/src/components/FundFlowPanel.tsx`，92 行版本）用的是三元链：

```tsx
{tab === "crowding" ? <SectorCrowdingTab />
 : tab === "etf"      ? <EtfFlowPanel />
 : tab === "rotation" ? <SectorRotationTab />
 : <FundFlowBoard tab={tab} />}
```

三元链的语义是"只渲染命中的那一个"—— 切走即卸载，**切回即重新挂载**，
组件内所有 `useState` 归零。而 `App.tsx:636-650` 里
`IntelPanel` 与 `AlertsPanel` 都有 `<KeepAlive>` 包着，
**`FundFlowPanel` 没有**（它走的是上面这条普通三元链）。

> ✅ **本会话期间该问题已被并发协作者修复**（`FundFlowPanel.tsx` 92 → 108 行，
> 源文件 mtime `2026-09-27 03:33`）。修法与本文 §2.3 的建议**完全一致**：
> 只给两个重页签接 `KeepAlive`，`FundFlowBoard` 与 `EtfFlowPanel` **维持卸载语义**
> （注释明确写了理由："各自的后台轮询不会在隐藏时继续跑"）。
> 已确认**构建并发布**：`web/dist` 与 `web/dist-pilot` 于 `2026-09-27 21:59:08`
> 同步构建，`FundFlowPanel` 被**代码分割**成独立分块 `FundFlowPanel-QyhHiG0_.js`（134 KB）。
> 详见 §3.4 的遗留问题——**线上是否真的生效，本会话未能验证**（原因见 §3.5）。

> ⚠️ 同一个坑，`web/src/components/KeepAlive.tsx:4-22` 已经**逐字记录过**
> （2026-09-26 用户第一次报障："切界面还是会有 2 秒延迟"），
> 但当时只修了情报流与事件告警两个面板，**资金流监控下的这两个页签没有跟上**。
> 这正是 `guard-fidelity-and-effect-verification` 说的"**改了 ≠ 生效了**"——
> 修法没有覆盖到全部同类现场。

**② 重新挂载就要重新取数，而公网每个往返都要 1~2 秒**

`SectorCrowdingTab` 一挂载就打 3 个接口（本地全部 ≤ 41 ms）：

| 接口 | 本地 | **公网实测** | 体积 (gzip) |
|---|---|---|---|
| `/sector_crowding/config_list?concepts_only=true` | 41 ms | **1.38 s**（抖动 **35.9 s**） | 34.6 KB |
| `/sector_crowding/sectors_max_ma5` | 21 ms | **4.47 s**（抖动 **超时 60 s**） | 31.3 KB |
| `/sector_crowding/metrics/summary` | 25 ms | 1~2 s | 389 B |

`SectorRotationTab` 挂载时打 `/sector_rotation/history`（**1.23 s**，本地 **8.8 ms**），
然后 **iframe 重新加载**报告：

```powershell
# 本机 17 ms / 15.5 KB(gzip)；公网 1.54 s / 3.25 s，抖动时 64 s 后失败
curl.exe -s -o rep.html -w "%{http_code} %{time_total}" -H "Accept-Encoding: gzip" `
  -H "Cookie: moss_sid=<sid>" "http://127.0.0.1:8110/api/v1/sector_rotation/report.html"
```

报告解压后 **58,181 字节**、含 **4 个 ECharts**，且第 3 行从 **CDN** 拉库：

```html
<script src="https://cdn.jsdelivr.net/npm/echarts@5/dist/echarts.min.js"></script>
```

**③ 这两个页签没有任何缓存或预取**

全局搜 `web/src` 无 `crowdingCache` / `rotationCache` 之类实现。
`web/src/panelPrefetch.ts` 那套保活续期（`KEEPALIVE_MS = 4 min`、
`visibilitychange` 跳过、`inFlight` 去重）目前**只覆盖 alerts + intel 两个目标**，
拥挤度/轮动完全没进去。

**④ 为什么当初故意没做（要说清，否则会改错地方）**

`FundFlowPanel.tsx:22-31` 写明了：拆出 `FundFlowBoard` 就是为了
**切走时卸载**资金流的 60 秒轮询。这个顾虑对"板块资金流/个股资金流"成立，
但被**一并套用**到了拥挤度/轮动上 —— 而这两个页签的取数是**一次性**的：

- `SectorCrowdingTab.tsx:57-60`：60 秒 `list.reload()` **兜底轮询**（低频）；
- `useCrowdingMetrics`：`metrics/summary` 只在挂载时取一次；
- `SectorRotationTab`：**完全没有轮询**。

### 2.3 修法

> ✅ **本节建议已由并发协作者实施**（见 §2.2 ① 的说明）。
> 下面保留原始分析与取舍，供 review 与后续同类问题复用。

**第一层（最小、最对症）：只给重页签加 KeepAlive，别包 `FundFlowBoard`**

```tsx
<KeepAlive active={tab === "crowding"}><SectorCrowdingTab /></KeepAlive>
<KeepAlive active={tab === "etf"}><EtfFlowPanel /></KeepAlive>
<KeepAlive active={tab === "rotation"}><SectorRotationTab /></KeepAlive>
{tab !== "crowding" && tab !== "etf" && tab !== "rotation" && (
  <FundFlowBoard tab={tab} />
)}
```

这样"卸载止损资金流轮询"的初衷**仍然保留**。

**实际落地的版本**（`FundFlowPanel.tsx:94-105`，比上面的建议更保守、更准确）——
`EtfFlowPanel` **也没有**保活，只保活报障点名的两个：

```tsx
{tab === "sector" || tab === "stock" ? <FundFlowBoard tab={tab} /> : null}
{tab === "etf" ? <EtfFlowPanel /> : null}
<KeepAlive active={tab === "rotation"}><SectorRotationTab /></KeepAlive>
<KeepAlive active={tab === "crowding"}><SectorCrowdingTab /></KeepAlive>
```

它同时触发了**代码分割**：`FundFlowPanel` 从主 chunk 拆成独立分块
`FundFlowPanel-QyhHiG0_.js`（134 KB），主入口 `index-C0-V1hDx.js` 降到 **264.7 KB**
——这对首屏是有利的（`性能硬约束` 要求"新面板必须代码分割**并且**把分块加进预热列表"，
分块预热见 `panelPrefetch-CYhCKlo2.js`）。

> ⚠️ 硬约束（`KeepAlive.tsx:24-34`）：`KeepAlive` **必须始终在树上、只切 `active`**，
> 不能塞进三元链 —— 否则它自己的 `useRef` 一起归零，保活完全失效。

**取舍必须说清**：保活会让拥挤度那两条低频轮询在后台继续跑
（约 1 次/分钟 × 34.6 KB；本地 41 ms、公网 1.4 s）。
收益是切回来**零等待**且数据是热的。想避开这个代价就走第二层。

**第二层（可选，要"完全零等待"再加）：预取 + 本地缓存**

- 给两个页签加 localStorage 缓存 + stale-while-revalidate
  （照 `intelCache.ts` / `alertsCache.ts` 同款：挂载先画缓存，再后台核对）；
- 把 `config_list` / `sectors_max_ma5` / `rotation history` 注册进
  `panelPrefetch.ts` 的 `prefetchPanels`，让后台续期一并覆盖。

**第三层（顺带，公网净收益）：ECharts 改自托管**

把 `report.html` 里 jsdelivr CDN 的 ECharts 挪进 `/assets/` 一起托管 ——
国内访问 jsdelivr 本就不稳，4 张图都要等它，且这是**外部往返**。

### 2.4 发布纪律（本项目真实踩过）

改完**必须**打真实产物验收，三步缺一不可：

```powershell
python manage.py build            # 构建到 web/dist
python manage.py ship-frontend    # 同步到 web/dist-pilot（对外实例指向的那份）
# 然后比对线上产物文件名与本地发布件是否一致，再打公网入口
```

依据：`manage.py:1081-1105`（`ship-frontend` 的定义）、
`manage.py:603`（`MOSS_WEB_DIST = web/dist-pilot`）、
`src/api/main.py:929-948`（托管目录与 `MOSS_WEB_DIST` 覆盖）。

> **本项目真实发生过**"源码早已优化并构建，但对外实例跑的是 6 小时前的旧构建"。
> **本地 dev 快不等于线上快。**

---

## 3. P3 —— 【会话末尾新发现，未结】对外实例进入"杀进程 / 值守重启"循环

> ⚠️ **本节是本文写作过程中（2026-09-27 22:56~23:02）现场发现的**，
> 与 P1/P2 无关，但**优先级最高**：它让站点处于"时通时断"。
> 我只做到"定位到循环 + 排除若干假设"，**根因未收敛**，如实登记。

### 3.1 症状

| 观测 | 结果 |
|---|---|
| 公网 `/` | **502**（1.3 s） |
| 公网 `/api/v1/health/live` | **000**（15 s / 40 s 超时）、**502** |
| 本机 8110 | **无监听**（`Get-NetTCPConnection -LocalPort 8110` 为空） |
| 连续 4 次探针（间隔 20 s） | 200 / 200 / **000** / 200 → **时通时断** |

### 3.2 循环的证据

`data/run/backend_incidents.jsonl` 共 **38 条**记录，末尾呈严格的周期形态：

```
22:57:07 restart     端口无监听（进程已消失）  mem_free 9.39 GB / 31.7 GB
22:57:16 restart_ok
22:57:22 restart     端口无监听（进程已消失）
22:57:26 restart_ok
22:58:08 restart     端口无监听（进程已消失）
22:58:16 restart_ok
22:58:52 restart     ...
22:59:00 restart_ok
23:00:07 restart     ...
23:00:16 restart_ok
```

**周期约 45~55 秒**：进程起来 → 存活 ~45 s → 被干掉 → 值守 1 分钟内发现并拉起。
即 **`MossPilotWatchdog`（每 1 分钟，`LastTaskResult=0`）一直在正常工作，
它在和一个"每分钟左右杀掉后端"的东西对抗**。

### 3.3 已排除的假设

| 假设 | 判据 | 结论 |
|---|---|---|
| 值守脚本坏了（BOM 丢失 → 解析失败静默） | `pilot_watchdog.ps1` 前三字节 `EF BB BF`，BOM 在位；`pytest tests/unit/test_ps1_encoding.py` **24 passed** | **排除** |
| 进程自身崩溃 / OOM | `backend.log` 尾部只有 `Application startup complete`，**没有 lifespan 关停日志**；`mem_free 8.79~9.39 GB` | **排除**（硬杀不走 lifespan，且内存充裕） |
| Windows 层 python 崩溃（0xc0000005） | 事件日志最近一条是 **2026-09-25**，不在本窗口 | **排除** |
| 值守自己 `taskkill` 了刚拉起的实例 | `manage.py:1616-1634` `_ensure_restart` **刻意不传 `--replace`**（注释：`--replace` 会按命令行枚举本项目**全部**后端进程，连 dev 8100 一起停） | **排除**（就代码意图而言） |
| 隧道侧问题 | `tunnel-watchdog.log` 明确写 **"主用不通且本机后端 8110 也不通，跳过隧道处置（归 PilotWatchdog）"** —— 隧道守值**正确地把问题归给了后端** | **排除** |

### 3.4 【高风险，需立刻处置】值守计划任务已变为 `Disabled`

会话末尾连续 3 次采样（间隔 5 s）均为：

```
TaskName       : MossPilotWatchdog
State          : Disabled          ← ★
LastRunTime    : 2026/9/27 23:01:01
LastTaskResult : 0
NextRunTime    : 2026/9/27 23:02:02
```

**含义**：当前能通只是因为**最后一次重启恰好成功**。
**一旦后端再次被杀，就没有任何东西会把它拉起来** ——
站点会从"时通时断"直接变成"永久 502"。

> 另注：`MossCloudflaredTunnel` 同样处于 `Disabled`（但这属既有状态，
> 对外链路当前走 frp/SSH 隧道，见 `frp-watchdog.log`）。

### 3.5 未结项与下一步

**未收敛的问题**：**"谁在每约 45 秒硬杀一次后端"** 尚未定位。
候选方向（按排查成本排序）：

1. 并发协作者正在反复 `manage.py stop/start` 或跑会重启服务的脚本
   （`data/run` 下有多个 23:01:48~23:01:50 新建的 python 进程，命令行为
   `manage.py start --env pilot --port 8110`）；
2. 某个按下标/按路径枚举并强杀的工具（本项目 `AGENTS.md` 明令
   **"停止按 PID 精确树杀，不用进程名通杀"**，说明这类工具历史上存在过）；
3. Job Object 关闭（父进程退出带走子进程）——
   `scripts/run_tests_in_job.py` 里有 `_create_kill_on_close_job()`。

**建议的下一步**（本会话未执行，因为要动生产）：

```powershell
# ① 先恢复值守，止住"一旦再挂就永久不通"的风险
Enable-ScheduledTask -TaskName "MossPilotWatchdog"

# ② 用进程创建/终止审计抓现行（而不是继续猜）
#    需要 Sysmon 或审核策略 Process Creation/Termination（事件 4689 含退出码）
Get-WinEvent -FilterHashtable @{LogName='Security'; Id=4689} -MaxEvents 20

# ③ 在 8110 的父进程链上确认：谁在杀它
#    本轮观测到的链：manage.py start --env pilot --port 8110
#                     └→ python(venv) └→ python -m uvicorn ... --port 8110
```

> **纪律提醒**（本项目已有教训）：**不要**在没定位到"谁在杀"之前
> 就用"改值守脚本"或"加 taskkill"来应对 —— 那正是
> `tunnel_watchdog.ps1:42-51` 记录过的死循环：
> "taskkill 全部 cloudflared + 自己 Start-Process 一个，于是形成死循环"。


---

## 3. P0 —— 公网链路本身（放大器，不在代码里）

### 3.1 数字

| 探针 | 结果 |
|---|---|
| `/health/live` × 5 | 1.19 / 1.45 / 4.51 / 5.05 / 1.56 s |
| `/health/live` 抖动 | **15.78 s**，其中 `time_appconnect`（TLS 握手）**15.05 s** |
| `/intel/calendar` × 5 | 2.0 / 5.0 / 18.6 / 34.6 / 61.5 s |
| `/intel/calendar` | **502**（Cloudflare 边缘够不到源站）8.56 s |
| `/sector_crowding/sectors_max_ma5` | 4.47 s，抖动时 **60 s 超时** |
| cloudflared 进程 | 2 个，启动于 2026-09-26 20:14（会话中途重启过） |

### 3.2 结论

**一次 cold 请求的固定开销已在 1~5 秒量级，且抖动幅度达 10 倍以上。**
这条链路上：

- **"体积"不是主要矛盾**（58 KB 与 15 KB 的耗时接近，"1.4 KB 也要 2.5 秒"）；
- **"次数"才是** —— 这正是项目 `性能硬约束` 里"减少的是次数，不是字节"的实测依据；
- **任何 < 6 秒的前端超时都是"必然误杀"**，因为在抖动窗口内它不可能成功。

### 3.3 修法方向（本轮未实施，仅登记）

1. 隧道健康纳入端到端判据（不能只看"端口在听""进程还在"）；
2. 抖动期给前端一个**显式的降级态**（"链路较慢，正在重试"）而不是静默丢弃；
3. 大 payload 接口一律**先回旧数据**（`_slow_payload` 的 stale-while-revalidate 已具备），
   避免抖动期把用户堵在"现拉"上。

---

## 4. 问题汇总表

| # | 问题 | 层 | 根因 | 修法 | 状态 |
|---|---|---|---|---|---|
| P1-① | 日历刷不出来 | 前端 | `LOAD_TIMEOUT_MS=6000` 按本机标定，公网 TTFB 达 6.8 s | 提到 20 s | **待改** |
| P1-② | 无任何错误提示 | 前端 | abort + 有 feed 缓存 → `errCal` 也不设 → 落到"暂无日程数据" | 日历单独补发/显式降级态 | **待改** |
| P1-③ | 缓存 6 h 到期即现拉 | 后端配置 | `INTEL_SLOW_CACHE_HOURS=6`，重建一次 13.6 s | 提到 24 h | **待改**（缓存已重建） |
| P1-④ | payload 偏胖 | 后端 | 2356 条事件、单条解禁带 40 只票全字段 | 瘦身/按需拉明细 | **待改** |
| P2-① | 切页签 1~2 s | 前端结构 | `FundFlowPanel` 三元链 → 卸载重建 | 重页签加 `KeepAlive` | ✅ **已修**（`FundFlowPanel.tsx:100-105`） |
| P2-② | 每次切都重取数 | 前端 | 两个页签无本地缓存、不在 `panelPrefetch` 覆盖内 | 缓存 + 预取 | **待改**（`KeepAlive` 已覆盖主症状） |
| P2-③ | iframe 重载 + CDN | 前端 | `report.html` 引 jsdelivr ECharts | 自托管 | **待改** |
| P0 | 公网 1~5 s 固定开销 | 基础设施 | Cloudflare/隧道 RTT、抖动、502 | 健康判据 + 降级态 | **登记** |
| **P3-①** | **对外实例时通时断** | **运维** | **每约 45 s 有东西硬杀后端 + 值守每 1 分钟拉起** | **先定位"谁在杀"** | ⚠️ **未结（最高优先级）** |
| **P3-②** | **`MossPilotWatchdog` 处于 `Disabled`** | **运维** | **未确认（会话末尾变化）** | **`Enable-ScheduledTask`** | ⚠️ **未结（高风险）** |

> 说明：
> - P1 / P2 / P0 均为"已定位到文件与行号、并给出可复现命令"的条目，
>   按用户口径"先诊断、后改动"，本轮**只诊断未改代码**（除重建过期缓存这一项运维动作）。
> - **P2-① 由并发协作者在本会话期间修复并构建**，本文补记了验证方式与遗留项。
> - **P3 是本文写作过程中现场发现的**，优先级高于以上全部：
>   它决定"站点此刻能不能打开"，且 **P3-② 一旦触发会让故障从"时通时断"升级为"永久 502"**。


---

## 5. 我自己的误判记录（必须留档）

> 这一节不是自我批评，而是**下一轮省时间的资产**：
> 下面每条都真实消耗了 2~5 个回合，且都是"看起来非常合理"的假设。

| # | 我的假设 | 为什么它很诱人 | 推翻它的那一次查证 |
|---|---|---|---|
| M1 | 浏览器跑的是旧 bundle，所以不调 `/bootstrap` | 日志里确实有旧签名请求；而 bundle 内容哈希肉眼不可比 | 把**线上 bundle 下载下来算 SHA256**，与 `dist-pilot` 逐字节比对 → **完全一致**，假设破产 |
| M2 | `dist-pilot` 与线上不一致（发布没同步） | 项目历史上真发生过 | 同上 SHA256 比对 + 公网 `index.html` 里的 asset 名 |
| M3 | 用户没带会话 Cookie（401 是元凶） | 日志里真有 5 次 401，其中一次来自用户 IP | 从 `moss_pilot.db` 取真实会话，用领域层 `validate_session` 验证 `valid: True`，且日志里该 IP 的 feed 请求全 200 |
| M4 | **`TestClient` 复现出"8143 ms"= 线上就慢** | 数字看起来很真实 | `TestClient` 进程的 **CWD 不同** → `DEFAULT_SLOW_DIR = Path("data/cache/intel")` 是**相对路径** → 缓存 miss → 每次现拉。**这个 8 秒是我自己制造的假象** |
| M5 | 用 PowerShell 读日志 grep "慢聚合预热" 判定"从没重建过" | 输出里确实出现乱码行 | 日志混有 GBK 字节，PowerShell 默认编码把中文行变成乱码 → **grep 漏命中**。改用 Python 按 `errors='replace'` 解码后计数 |
| M6 | 缓存 6 小时没被后台预热刷新 = 预热循环有 bug | 时间窗口与 TTL 完全吻合 | 读到日志 `情报流预热循环已启动，本次跳过：非工作日` → 循环**在跑**，`should_fetch` 按工作日短路；`prewarm_slow` 的重建路径需要**逐条**验证（本轮未收敛，**如实标为未结**） |
| M7 | 用户 IP 是 `139.227.111.134` | 它出现在很多请求里 | 会话表里该用户的 `ip` 是 IPv6 `2408:820c:...`；把两者混为一谈导致前半程时间线读错 |

**M4 与 M5 的共性**：**我的测量工具本身引入了误差，而我把误差当成了被测对象的性质。**
这与 `ai-interaction-and-edit-safety` 里"自己的检查脚本必须先自证"、
`ai-first-pass-defect-guard` 里"优化前后对比必须先清测量路径"是同一个坑的第三次复现。

---

## 6. 可复用的三条判据（本轮沉淀）

1. **分层计时，一次只动一层。**
   本机（后端）→ 公网（链路）→ 前端（渲染逻辑），**三段分别打点**。
   本会话正是因为"本机 40 ms"与"公网 61 s"被放在同一张表里，才一眼看出病灶不在后端。

2. **"有缓存"会掩盖失败，排查时必须先问"这条路径有没有别的东西兜着"。**
   情报流有 localStorage → 失败被遮住；日历没有 → 裸奔。
   **同一时刻两个面板表现不同，不代表有两个 bug。**

3. **判据要写成"次数 / KB / 布尔"，不要写毫秒。**
   毫秒换条线路就不成立；本会话所有关键结论都能重新表达为
   "**1 次往返 / 58 KB / 是否命中 KeepAlive**"。

---

## 7. 关联文档

| 主题 | 文档 |
|---|---|
| 前端改动的 16px 渐隐带 / flex 挤压 / 视图键三处枚举 | `.trae/skills/frontend-change-guardrails/SKILL.md` |
| 端到端时延预算表、串行往返上限 | `.trae/skills/e2e-latency-budget/SKILL.md` |
| "改了 ≠ 生效了"四道门 | `.trae/skills/guard-fidelity-and-effect-verification/SKILL.md` |
| 感官延迟与请求耗时混为一谈 | `.trae/skills/perceived-latency-triage/SKILL.md`（**本会话新建**） |
| 保活与预取的设计 | `docs/PANEL_PREFETCH_KEEPALIVE.md` |
| 投资日历口径与数据源 | `docs/INVESTMENT_CALENDAR_DESIGN.md` |
| 板块拥挤度指标口径 | `docs/SECTOR_CROWDING.md` |
