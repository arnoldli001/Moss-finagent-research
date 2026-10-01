# 本轮会话问题清单 · 根因 · 解决方案

**时间**：2026-09-26 深夜 ~ 2026-09-27 午后（迭代日期标注：仓库内代码注释本轮记为
`2026-09-27` 与 `2026-09-28` 混用；按日期检索时两者都看。机器时钟为 2026-09-27。）

**范围**：从"情报流首屏 5~10 秒"开始，到"一级目录页签 3~5 秒才长齐"结束，
共 14 条问题；另有 **23:00 复查验收证据时当场发现的 P15**（对外 502，根因是
值守把"忙"判成"死"），见 §7。横跨前端首屏、认证链路、代理与隧道、
Windows 平台行为、构建发布、多租户权限下发、**值守判据**七个面。

---

## 0. 怎么读这份文档

每条问题的结构固定为：

```
症状 → 定位过程（含被推翻的假设） → 根因 → 修法 → 证据 → 复发防线
```

三条阅读约定：

1. **判据一律写"次数 / KB / 成功率 / 最差值"，不写毫秒。**
   毫秒换条线路就不成立；次数与 KB 才可 review、可断言。本轮的时延数字一律附带
   "在 HK 这条链路上实测"，因为这条链路一次冷请求的**固定开销约 0.85~0.9 秒**，
   且与体积几乎无关（15 KB 分块 0.93s、64 KB 分块 1.12s）—— 所以优化对象是**次数**。
2. **被推翻的假设要留下。**本轮有 3 个假设是先立后破的（P05、P10 的归因、
   P08 的"未知等级→空清单"）。它们比结论更有教育意义，因为下一步动作完全相反。
3. **未定位的写"未定位"**，不留"可能是……"的模糊结论（见 §4）。

---

## 1. 总览

| # | 症状 | 根因（一句话） | 修法 | 关键证据 |
|---|---|---|---|---|
| P01 | 情报流首屏 5~10 秒 | 日历/热度**现算**（11.1s / 3.5s）+ 前端串行等待 | 慢盘缓存 + `/intel/bootstrap` 合并 + 前端解耦 | `/intel/calendar` 11,133 ms、`/intel/heat` 3,472 ms |
| P02 | 刷新时"正在检查登录状态"约 10 秒 | 三次**串行**公网往返（`/me` 401 → `/refresh` → `/me` 200） | `/auth/bootstrap` 一次往返答完 | 单次往返实测 0.4~2.0s |
| P03 | 事件告警首次打开 2~3 秒 | 预取只写在 `login()` 里；**记住我刷新**那条路径从不预取，且缓存有 TTL | `/alerts/bootstrap` + 常驻保活（续期间隔 < TTL） | 续期 4 min < 最短 TTL 5 min |
| P04 | 缺"多/空"筛选 | 方向由模型判定但没暴露成筛选维度 | `/intel/feed?direction=` + 方向选择器；判定**唯一实现** | 13 条单测 |
| P05 | 孤儿 pytest + 后端被静默杀 | 工具调用启动的子进程绑定父句柄/Job；无值守、无现场 | Job Object 化测试 + `ensure` 值守 + 事故流水 | commit 49.05/63.74 GB = 77%，**非 OOM** |
| P06 | CF 隧道不稳 / 双实例 / 重启无效 | 脚本 `taskkill` 与 SCM 20 秒自恢复**叠加** → 两个连接器 | 改走 Windows 服务，脚本不再自起进程 | `tunnel list` 4 条 vs 8 条连接 |
| P07 | 香港 VPS 迁移 | 腾讯云安全组 `YJ-FIREWALL-INPUT` 只放行 22/80 | frp over SSH(22) → 开 443 → nginx TLS → certbot | 443 先 `timeout` 后 `refused` |
| P08 | 一级目录 3~5 秒才长齐 | 页签清单**只能等服务端**，且该请求排在认证之后 → 两次串行 | ① 清单并进 `/auth/*` ② 按用户本地缓存 ③ 后台校验 | 13 条测试三处逐字比对 |
| P09 | CF 路径偶发 20 秒卡死 | CN→CF 的 IPv6 **握手成功但大包被丢**（ICMPv6 全尺寸无响应） | 关 CF「IPv6 兼容性」（待办）；HK 无 AAAA 不受影响 | `-6` 3/8 成功；`-4` 20/20 |
| P10 | HK 首字节 20 秒不来 | Windows `SO_REUSEADDR` 允许**重复绑定** → 两个隧道同时监听 17000 | 改 `SO_EXCLUSIVEADDRUSE` | 实例 B 自行退出 `WinError 10048` |
| P11 | 看门狗从未运行；任务=1 却无日志 | `.ps1` 缺 UTF-8 BOM → PS 5.1 按 GBK 读 → **解析失败即退出** | 补 BOM + 三重护栏测试 | frp 9 个语法错、tunnel 4 个、pilot 0 个 |
| P12 | 每 5 分钟弹 PowerShell 窗口 | `-WindowStyle Hidden` 来不及（控制台在 `CreateProcess` 阶段就分配）；WMI 默认新控制台；重复的登录任务 | `wscript` + `SW_HIDE`；`CreateFlags`；禁用重复任务 | 被动监视抓到标题=exe 全路径 |
| P13 | pilot 跑的是 6 小时前的旧构建 | `ship-frontend` 是**有意的人工步骤**，源码改了但没 ship | `build` → `ship-frontend` → 打公网验收 | 828 KB 单文件 vs 257 KB + 分块 |
| P14 | 二级页签比一级页签还大 | `.quant-module` 14px/7×18 对 `.tab` 13px/6×14，**层级倒挂** | 12px/4×12 + 背景区分 | 构建产物 CSS 逐值比对 |

---

## 2. 逐条复盘

### P01 情报流首屏 5~10 秒

**症状**：客户打开「热点&研报小作文」，首屏空白 5~10 秒。

**定位**：直接量服务端——`/intel/calendar` **11,133 ms**、`/intel/heat` **3,472 ms**；
而同一时刻 `/api/v1/health/live` 只有 2~3 ms。结论：不是网络，是**服务端现算**。

**根因**：日历与热度是"请求到来时才去上游拉+算"，没有落盘缓存；前端又把它们
串在处理链上，于是首屏时间 = 最慢那个接口的时间。

**修法**：
- 慢接口结果落盘缓存（`intel_slow_cache_hours`，含"正在构建"占位与后台续算
  `_spawn_slow_renew` / `_SLOW_BUILDING`，避免并发重复构建）；
- 新增 `GET /intel/bootstrap` 一次取齐 `{feed, calendar}`，并把路由**注册在
  `/intel/item/{content_hash}` 之前**（FastAPI 按注册顺序匹配，否则 `bootstrap`
  会被当成一个 `content_hash`）；
- 前端把"日历"与"情报流"拆成互不阻塞的两块。

**证据**：11,133 ms → 命中缓存后与 health 同量级。**注意缓存键必须含全部维度**——
本轮就踩到：`feed_cache_key` 加了 `|direction` 之后，热加载回填时按新键算会写成
`59||...` 而不是 `60||...`，修法是回填时**重算键**并把 `limit` 写进 payload。

**复发防线**：`tests/unit/test_intel_slow_cache.py`（9 条）。

---

### P02 刷新时"正在检查登录状态"约 10 秒

**症状**：刷新页面后，登录态检查要停约 10 秒。

**根因**：前端串了三次公网往返：`/me`（401）→ `/refresh`（200）→ `/me`（200）。
单次 0.4~2.0 秒，三次叠加就是用户看到的 10 秒。其中第三次是**纯浪费**：
`/refresh` 的响应里本来就带了 `user`。

⚠️ **顺序约束没有消失，只是搬到了服务端**：`refresh` 会**轮换** remember token，
所以不能无条件先 refresh —— 一旦某次响应丢失，客户端手里的旧令牌就成了"重放"，
服务端会**撤销整个令牌家族**，用户被强制重登。

**修法**：`/auth/bootstrap` 一次往返答完"我是谁 + 要不要图形码"，
服务端内部保证"先看会话、确实没有才续期"，客户端无法用错顺序。

**证据**：本机 1~62 ms；公网单次 0.4~2.0 s。三次 → 一次。

---

### P03 事件告警首次打开 2~3 秒

**症状**：用户原话"每次打开这个界面不能预加载到浏览器吗，切界面还是会有 2 秒延迟"。

**定位（第一轮已修过但没修好）**：上一轮加了 `localStorage` 缓存 + stale-while-revalidate，
**仍然慢** —— 因为缓存是**空的**。两条根因：

1. 预取只写在 `useAuth.login()` 里，而绝大多数访问是**带 remember-me Cookie 刷新页面**，
   走的是 `probe() → /auth/bootstrap`，那条路径**从不预取**；
2. 缓存有 TTL（告警 5 分钟 / 情报 10 分钟），用户在别的页面待够 TTL 就作废。

**修法**：把"预取"变成**常驻保活**（`panelPrefetch.ts`）：
- 只要处于已登录就预取一次，之后按 `KEEPALIVE_MS = 4 min` 续期 —— **必须小于**
  最短 TTL（5 min），否则续期之间会出现"缓存刚好过期"的窗口；
- 续期用 `force=true` 绕过新鲜度去重，否则第二次起被判"还新鲜"直接返回、缓存永不更新；
- **不并发**（`inFlight` 去重）、**页面隐藏时跳过**（用户没在看，白花带宽）、
  失败静默（预取是增益，不是功能）。

**证据**：`/alerts/bootstrap` 一次往返取齐列表+设置；`/intel/bootstrap` 同上。

---

### P04 缺"多/空"筛选

**根因**：方向是模型分析出来的（每条信息第一个字【空】/【多】），但没成为筛选维度。

**修法**：`/intel/feed` 增加 `direction=all|bull|bear`（非法值 422）；
**判定只允许一份实现** —— `direction_of()` 与 `_has_direction()` 都委派给
`alert_bridge`（语义：neutral 压制 → has_tone → 字面 偏多/偏空 兜底）。
这与 AGENTS.md 的"同一判断只允许一份实现"是同一条纪律。

**复发防线**：`tests/unit/test_intel_direction_filter.py`（13 条）。

---

### P05 孤儿 pytest 与"后端被静默杀"

**症状**：一个 `python -m pytest` 变成孤儿（PID 18108，CPU=0、HandleCount=0、父进程已死）；
另一件事是客户先发现网站打不开、我们后知后觉。

**定位**：**不是 OOM**。commit 49.05/63.74 GB = 77%，物理内存余 14.78 GB；
服务不在任何 Job Object 里；无崩溃日志、机器没装安全软件。所以"外部 `taskkill /F`"
是唯一剩下的解释，但**真凶未锁定**（见 §4）。

**修法**：
- 测试改为在 Job Object 里跑（`scripts/run_tests_in_job.py`），父进程死了不会留孤儿；
- `manage.py ensure` 做后端值守：记录 `memory_snapshot()` + 原因到
  `data/run/backend_incidents.jsonl`，把"客户先发现"变成"一个周期内自动恢复 + 现场入流水"；
- 明确拒绝把 Ollama 改成按需加载（用户约束：被定时任务大量使用，冷启动会让体验更差）。

**方法论收获**：`Start-Process -RedirectStandardOutput` 启动的子进程会绑定父句柄，
调用者一退出就被带走；必须用 `Win32_Process.Create`（父进程变成 `WmiPrvSE.exe`）。
但**它也有代价**，见 P11/P12：不继承环境变量、默认分配可见控制台。

---

### P06 Cloudflare 隧道不稳 / 双实例 / 重启无效

**症状**：隧道 1.4~4.9 秒是常态、偶发 15~30 秒空窗；用户报"两个 cloudflared 同时在跑"。

**根因（两段）**：
1. 第一版 `tunnel-ensure` 把任何非 200 当失败 → 探测路径被登录门槛拦下时**误判为故障**，
   白白重启（重启会中断所有在途请求）；
2. 第二版改成 `taskkill` 全部 cloudflared + 自己 `Start-Process`。而 cloudflared 在这台
   机器上是**以 Windows 服务方式运行**的（`StartMode=Auto`、
   `FAILURE_ACTIONS = RESTART -- Delay = 20000 ms`）—— 于是
   **杀掉 → SCM 20 秒后拉起（PID A）→ 我们又起一个（PID B）= 两个实例**，
   各自向 CF 注册连接器（实测相差 16 秒）。**重启本身成了不稳定源。**

另有一个假结论要记录：`restart_ineffective` 是**误判** —— SCM 恢复有 20 秒延迟，
而代码只等了 25 秒就下结论。

**修法**：走 Windows 服务（`Restart-Service` / `Start-Service`），由 SCM 保证
"永远只有一个实例"；包装脚本**绝不自起 cloudflared 进程**。探测窗口也从 3×2 秒
放宽到 4×5 秒（验证过一次真实案例：第 1 次 `TimeoutError(20531ms)`、
第 2 次 HTTP 403 = 健康）。

---

### P07 香港 VPS 迁移

**症状/障碍**：腾讯云轻量主机 + frp 走不通：443/7000 一律超时。

**定位**：`YJ-FIREWALL-INPUT`（腾讯云安全组）**先于** ufw 生效，只放行 22/80。
所以 VPS 里 `ufw allow 443` 完全没用。

**修法（每一步都留了证据）**：
1. 先用 **frp over SSH**（`scripts/frp_ssh_tunnel.py`，paramiko 本地转发
   本机 17000 → VPS 127.0.0.1:7000），**不动控制台**即可打通；
2. 之后在控制台开 443，判据是"**先 timeout、后 refused**"—— refused 说明包已到达主机、
   只是没有监听者，据此确认放行生效；
3. nginx 加 TLS + certbot（ACME 走 80，`/.well-known/` 必须留在 80 段）；
4. nginx **必须**补 `X-Forwarded-For`，否则后端把所有用户看成本机地址，
   **登录限流与图形码判定全部失效**（见 P08 的 `client_ip`）。

**踩坑清单**：VPS 用户是 `ubuntu` 不是 `root`；`.ps1` 含非 ASCII 必须是
**UTF-8 with BOM**（见 P11）；`Start-Process` 起的进程活不过调用者（见 P05）。

---

### P08 一级目录页签 3~5 秒才长齐

**症状（用户原话）**："首次登录进去，一级目录只显示投资日历、事件告警，而投研分析、
策略回测、量化交易、主线挖掘、资金流监控、热点&研报小作文都要等 3-5 秒才出来"。

**根因**：页签清单**只有一个来源** `GET /me/features`，而它必须等认证结果就位才发得出去 ——
**两次串行往返**。这条链路一次冷请求约 0.9 秒，再叠加首屏入口下载，正好是 3~5 秒。

**修法（用户选定 ①+②，已实现并上线）**：

| 顺序 | 来源 | 时机 |
|---|---|---|
| ① | `/auth/login` 与 `/auth/bootstrap` 响应**顺路带回** `visible_views` | 认证那一趟，**不新增往返** |
| ② | `localStorage` 按用户缓存的整份 `MyFeatures` | **同步读，首帧** |
| ③ | `/me/features` | 挂载后后台**校验**，不再阻塞渲染 |
| ④ | `MINIMAL_VIEWS` | 前三者都没有时才用 |

结果：**首次登录 = 1 次往返**（原 2 次）；**之后每次刷新 = 0 次**。

**关键设计点**：后端把算法抽成唯一纯函数 `visible_views_for_tier()`，
`/me/features` 与 auth 两条路都调它 —— 因为"两份实现分叉"是这个修法**唯一的风险**，
症状是"刚登录时少一个页签、刷新一下又有了"，只在时序里出现。`_me_payload` 是
`/me` 与 `/auth/bootstrap` 共用的，改一处两条路同时生效且保持"逐字段一致"。

**②的边界**：按 `user_id` 分键（换人读不到上一个人的）、登出即清、
**空清单按"没拿到"处理**（服务端身份异常时算出来就是空，那不等于"一个页签都没有"）。

**一个被推翻的假设**：我原以为"未知等级 → 空清单"，测试跑出来是 5 个页签 ——
`get_platform_config().plan("no-such-tier")` **不抛异常**，而是**回落默认档**。
真正 fail-closed 的是"配置读不到"那条分支。测试已按真实契约改，并补了一条专门
打中该分支的用例（配置抛异常 → 必须空清单，**不能全开**）。

**复发防线**：`tests/unit/test_nav_views_single_source.py`（13 条），核心是拿
`admin/vip/trial` 三种身份把**三处响应逐字比对**。

---

### P09 Cloudflare 路径偶发 20 秒卡死（IPv6）

**症状**：CF 路径 A/B 里 5/20 失败，失败样本 `time_connect=0` 且卡满 21 秒。

**定位**：verbose 显示 curl **先试 IPv6**（`Trying [2606:4700:3033::ac43:abcf]:443`）
再回落 IPv4 才成功。分家族实测：

| IPv6 目标 | 结果 |
|---|---|
| 淘宝 / 京东 | 200 / 0.16s、0.12s（国内 IPv6 飞快） |
| **Cloudflare** | 200 但 `total=10.0s`（顶满超时） |
| **`moss.wujiaitool.cn`（-6）** | **3/8 成功**，失败全是 12s 超时 |
| `moss.wujiaitool.cn`（-4） | **20/20**，中位 0.98s |

**根因（关键在失败形态）**：**TCP 握手是成功的（0.28s），数据却永远不来**。
这不是黑洞 —— 黑洞会让 Happy Eyeballs 立刻回落 IPv4；**握手成功但传输卡死，
浏览器根本没有回落的理由**。IPv6 路径 MTU 探测显示 ICMPv6 到 CF
**全尺寸无响应**（连 1232 都不回），说明 PMTUD 的"包太大"消息传不回来，
大包被静默丢弃 —— 与"握手包小（通）、证书包 3~4 KB（丢）"完全吻合。

**修法**：关掉 Cloudflare 的「IPv6 兼容性」（网络设置，两下点击）。
HK 入口没有 AAAA 记录，天然不受影响。

**这条也是"修正早期结论"的一次**：我最初把 CF 的不稳定归因于隧道本身，
剔除 IPv6 因素后发现 CF 走 IPv4 是 20/20、中位 0.98s ——
**CF 的"不稳"主要是 IPv6 造成的假象**。

---

### P10 香港路径首字节 20 秒不来（Windows 端口语义）

**症状**：HK 入口成功率 16/20，失败样本 `time_connect=0.18s` 正常、
然后**首字节 20 秒不来**（看起来像后端挂了，其实不是）。

**定位**：进程列表显示**两个隧道实例同时活着**，都认为自己绑定成功：

```
23172 → 22996   创建于 00:14:07     ← 真正持有 17000 的
11412 → 19084   创建于 00:14:20     ← 我启动的，绑定也"成功"了
```

**根因**：`frp_ssh_tunnel.py` 里写的是
`sock.setsockopt(SOL_SOCKET, SO_REUSEADDR, 1)`。而**Windows 与 Linux 的
`SO_REUSEADDR` 语义相反**：

- Linux：只允许绑定处于 `TIME_WAIT` 的端口，对"已有进程正在 LISTEN"仍报 `EADDRINUSE`
  → 脚本里那段"端口被占就退出"的保护**有效**；
- Windows：**允许重复绑定**一个正在 LISTEN 的端口，后绑定的照样成功 → 保护**完全失效**。

于是新连接被 Windows 随机分给其中一个，分到 SSH transport 已失效的那个就**永久挂住**。
更坏的是它让"重复实例"永远无法被自愈脚本发现（端口一直在听）。

**修法**：Windows 用 `SO_EXCLUSIVEADDRUSE`（独占绑定）。

**证据**：故意再起一个实例，它**自行退出**：

```
[tunnel] 本地端口 17000 已被占用（[WinError 10048] …只允许使用一次。）
```

修前 16/20、最差 20.0s ×4；修后 **30/30、中位 0.93s、最差 1.85s**。

**抢跑成因（值得记）**：00:14:07 那次是**看门狗自己拉起的**，我 00:14:20 又拉了一个 ——
**两个自动化/人同时"确保"同一个东西**，就是重复实例的来源。

---

### P11 `.ps1` 丢 BOM：看门狗从未运行

**症状**：计划任务 `LastTaskResult = 1`（看起来像业务失败），
但脚本的日志文件**一行都没写**；而它的设计是"健康时静默"——
于是"脚本已死"和"一切正常"**长得一模一样**。

**定位**：`[System.Management.Automation.Language.Parser]::ParseFile()` 直接给出答案：
`frp_watchdog.ps1` **9 个语法错**、`tunnel_watchdog.ps1` **4 个**、
`pilot_watchdog.ps1` **0 个**（只有它带 BOM）。报错里 `鏉庢旦_AI…`
（"李浩_AI…"被按 GBK 错解的乱码）直接指认了编码问题。

**根因**：Windows PowerShell 5.1 在文件**不带 UTF-8 BOM** 时按系统 ANSI（本机 GBK）
读取源文件。中文注释被错解后与相邻字符错位 → **解析失败 → 脚本在入口处就死掉**，
死在写日志之前。

**代价**：`MossTunnelWatchdog` 一直返回 1，意味着 **Cloudflare 那条链路长期没有值守** ——
这正是用户反复遇到的"后台挂了、要等很久"的根源。

**修法**：补 UTF-8 BOM；并加**护栏测试** `tests/unit/test_ps1_encoding.py`：
① 含非 ASCII 的 `.ps1` 必须有 BOM；② 所有 `.ps1` 必须能被解析器零错误解析；
③ `.vbs` 启动器必须保持纯 ASCII。它当场又抓出 `tunnel.ps1`、`tunnel-named.ps1`
两个同样的病号，后来又抓出另一位协作者新建的三个临时脚本（其中一个已完全解析不了）。

⚠️ **我自己又踩了一次**：用 `edit` 改 `frp_watchdog.ps1` 时 BOM 被吃掉，
`MossFrpEnsure` 下一次运行立刻从 `0` 变成 `1`。**BOM 是文件的隐藏属性、diff 里看不见**，
任何"读进来再写回去"的工具都可能丢它 —— 所以必须有自动检查，不能靠人记住。

---

### P12 每 5 分钟弹 PowerShell 窗口

**症状**：用户报"还是一直有 powershell 窗口定期弹出来"（第一次修完仍在）。

**定位（第一轮修错了）**：我先按"缺 `-WindowStyle Hidden`"修了 `MossFrpEnsure`，
再手动触发三个任务做对照实验，结果**零可见窗口**，差点据此结案。
改用 **5 分钟纯被动监视**才抓到真实现场：

```
pid=15604 powershell  标题='C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe'
父=1624(svchost)      命令行="powershell.exe" -NoProfile -NonInteractive -WindowStyle Hidden …
```

**根因**：`-WindowStyle Hidden` 的作用时机是错的 —— 控制台窗口由操作系统在
`CreateProcess` 内部就分配好了，**PowerShell 要等自己被加载起来才有机会去隐藏它**。
标题还是 exe 全路径，正说明抓到的是**启动瞬间**。另外两个来源：
`Win32_Process.Create` 默认按"新控制台"建进程（每次自愈弹两个黑窗）；
以及一个**重复的登录任务**（与 Windows 服务指向同一个隧道 `moss-pilot`，
UUID `6e41d39f-…` 一致，`tunnel list` 可见 4 条 vs 8 条连接）。

**修法**：
- 任务动作改成 `wscript.exe "...\frp_watchdog_hidden.vbs"` —— wscript 是 GUI 子系统
  程序、本身没有控制台，`Run(cmd, 0, True)` 在 `CreateProcess` 阶段就带 `SW_HIDE`，
  窗口**从未可见过**（而不是"先建出来再藏起来"）；退出码照样回传给 `LastTaskResult`；
- `Win32_Process.Create` 显式带
  `CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP`（实测 `MainWindowHandle`
  19270988 → 0）⚠️ **不要用 `DETACHED_PROCESS`，它会抵消 `NO_WINDOW`**；
- 禁用重复的 `MossCloudflaredTunnel`（服务已是 `Auto` + SCM 自恢复）。

**一条走过并被否决的路（已写进脚本注释）**：把任务改成 `UserId=SYSTEM`
确实让窗口消失（会话 0 没有桌面），但 `Path.home()` 变成 `config\systemprofile`
→ 找不到 SSH 密钥 → 隧道退出 → 入口 502；而且
**`Win32_Process.Create` 的子进程由 WMI 提供者创建，不继承调用者的环境变量**，
所以在脚本里设 `$env:VPS_KEY` 根本传不下去。已回退并把结论留在注释里，
免得后人重走。

**验收**：340 秒被动监视、且**恰好覆盖一次真实定时触发**（08:24:24，结果 0）
→ **可见窗口 0 个**。

**方法论教训**：`Start-ScheduledTask` 手动触发与 svchost 真实定时触发**不等价**。
验证必须走真实触发路径。

---

### P13 pilot 一直在跑 6 小时前的旧构建

**症状**：源码已优化、`web/dist` 也构建过了，但对外实例的行为没变。

**根因**：`ship-frontend` 是**有意设计的人工步骤**（先删后拷，保证 `dist-pilot`
与本次构建逐文件一致）—— 目的是"本地 `npm run build` 一跑完，客户刷新一下就是
那份还没验过的界面"这种事故。代价是：**构建了不等于发布了**。
本轮实测：`dist-pilot` 里是 00:13 的单文件 828 KB 构建，
而源码在 03:33 就做了代码分割、`dist` 在 03:54 就构建好了。

**修法**：走完整流程 `manage.py build` → `manage.py ship-frontend` → **打公网入口验收**。

**效果（HK 实测，gzip）**：入口 JS **258.6 KB / 2.29s → 85.0 KB / 1.15s**。

**但代码分割有代价**：第一次点某个面板要现下它的分块（这条链路上一次冷请求
固定 ~0.9 秒），会看到 Suspense 的"加载中"。所以**分割与预热是一对，必须同时做** ——
补了 `warmPanelChunks()`：登录后**空闲时**（`requestIdleCallback`，兜底 1.5s）
把 8 个业务面板分块**串行**拉下来（并发只会互相挤占同一条隧道）。
放在 `panelPrefetch.ts` 的 `startPanelKeepAlive()` 里调用，**刻意不改 `App.tsx`** ——
少改一个文件就少一份与并发协作者冲突的面。

---

### P14 二级页签比一级页签还大

**症状/根因**：用户要"二级页签比一级小一号、背景也要区分"。查下来不是"没区分"，
而是**层级倒挂**：

| | 字号 | 内边距 | 圆角 | 背景 |
|---|---|---|---|---|
| 一级 `.tab` | 13px | 6px 14px | 16px | `transparent` |
| 二级 `.quant-module`（改前） | **14px** | **7px 18px** | 18px | `transparent` |

**修法**：二级改 12px / 4px 12px / 12px 圆角 + accent 淡填充
（`background: rgba(74,158,255,0.10)` = `#4a9eff1a`）与同色描边。
于是"一级 = 透明描边胶囊，二级 = 有底色的实心小胶囊"，
**不看字号也能看出谁从属于谁**。

**副作用排查**：先 grep 确认 `.quant-module` 全项目只被 `QuantTabContainer.tsx`
使用（`IntradayTPanel.tsx` 只在注释里提到），改动无外溢。

---

## 3. 跨问题的共性根因（比单条更值钱）

1. **串行往返是首恶，而优化对象是"次数"不是"字节"。**
   P02（3 次→1 次）、P03（0 次预取→常驻保活）、P08（2 次→1 次→0 次）是同一个病。
   这条链路上一次冷请求固定 0.85~0.9 秒且与体积几乎无关 —— 所以判据必须是次数与 KB。
2. **假判据比没有判据更危险。**"端口在听"（P10）、"进程还在"（P06）、
   "任务返回 0"（P11）都曾经被判成健康。健康判据必须**端到端**，
   而且"健康时静默"必须与"脚本已死"**可区分**。
3. **平台语义差异是静默的。** `SO_REUSEADDR`（Windows 与 Linux 相反）、
   `DETACHED_PROCESS` 抵消 `CREATE_NO_WINDOW`、控制台窗口在 `CreateProcess` 阶段分配、
   `.ps1` 的 BOM —— 四条都不会报错，只会"表现成别的问题"。
4. **文件级隐藏属性与"构建 vs 发布"是同一类问题**：都不可见、都不在 diff 里、
   都只能靠自动检查（护栏测试 / 打公网验收）发现。
5. **一份实现。** 客户端 IP 三份实现已经开始分叉；页签清单必须唯一实现；
   方向判定委派给单一来源。这是 AGENTS.md 已有的纪律，本轮三次验证了它的必要性。
6. **先查最便宜的判据，再动手。** 本轮两个假设（`manage.py` 误杀、
   未知等级→空清单）都在"查一下"这一步就被推翻了 —— 幸好没直接改。

---

## 4. 遗留与未决（诚实清单）

| 项 | 状态 |
|---|---|
| 隧道与 frpc 在 01:14–01:18 之间**一起消失**的真凶 | **未定位**。我怀疑过 `manage.py` 按项目路径误杀，但 `is_our_cmdline` 只匹配 `src.api.main:app`，假设被推翻；只有"外部强杀"这一解释（`frp_tunnel.log` 无退出记录）。现在 `MossFrpEnsure` 已能正常工作，HK 路径每 5 分钟自愈一次（实测故障到恢复 15.3 秒） |
| 全量测试 12 条红灯 | **既有**，非本轮引入。9 条在告警阈值/通知（`.env` 的 `alert_confidence_min=0.6` 与测试断言的 0.7 漂移），3 条在密码哈希后端、LLM 路由档位、FedWatch 连接器。按 AGENTS.md 的"红灯纪律"，这 12 条都不该被当成背景噪音，需要各自"修好／删除并说明／标记为预期差异并链接待办" |
| Cloudflare「IPv6 兼容性」 | **未关**（需在控制台操作）。这是 P09 的根治手段 |
| `MossCloudflaredTunnel` | 已禁用（重复实例 + 登录时弹窗）。恢复命令：`Enable-ScheduledTask -TaskName MossCloudflaredTunnel` |
| `docs/OPS_GUIDE.md` / `HK_VPS_MIGRATION.md` | 本轮根因**尚未合并进这两份运维文档** |
| 日期标注 | 本轮代码注释里 `2026-09-27` 与 `2026-09-28` 混用（机器时钟是 09-27，仓库既有标注是 09-28） |

---

## 5. 本轮变更清单

| 文件 | 改了什么 | 为什么 |
|---|---|---|
| `scripts/frp_ssh_tunnel.py` | `SO_EXCLUSIVEADDRUSE`；密钥候选 `_candidate_keys()` | P10；不再依赖运行身份的 `Path.home()` |
| `scripts/frp_watchdog.ps1` | 判据改为**端到端公网入口**；实现 `Stop-AllTunnels`；补 `Backend` 守卫；`CreateFlags`；留否决记录 | P10/P11/P12 |
| `scripts/frp_watchdog_hidden.vbs`（新增） | `wscript` + `Run(cmd, 0, True)` 隐藏启动 | P12 |
| `scripts/tunnel_watchdog.ps1`、`tunnel.ps1`、`tunnel-named.ps1` | 补 UTF-8 BOM | P11 |
| `tests/unit/test_ps1_encoding.py`（新增） | BOM + 解析 + `.vbs` 纯 ASCII 三重护栏 | P11，防复发 |
| `src/api/routes/auth.py` | `_features_payload()`；登录响应与 `_me_payload` 下发 `visible_views` | P08 ① |
| `src/api/routes/my_features.py` | 抽出唯一实现 `visible_views_for_tier()` / `build_visible_views()` / `is_admin_tier()` | P08，防两份实现分叉 |
| `web/src/featuresCache.ts`（新增） | 按用户缓存整份 `MyFeatures`；`seedFeaturesViews`；空清单按"没拿到" | P08 ② |
| `web/src/hooks/useFeatures.ts` | 三级来源：本次校验 > 缓存 > 种子 > 最小集合 | P08 |
| `web/src/hooks/useAuth.ts`、`App.tsx`、`api.ts` | 开机探测/登录后落盘；登出清理；传 `user_id`；类型补字段 | P08 |
| `tests/unit/test_nav_views_single_source.py`（新增） | 三处响应逐字比对；管理员边界；fail-closed | P08 防复发 |
| `web/src/styles.css` | 二级页签 12px/4×12 + 背景区分 | P14 |
| `web/src/panelPrefetch.ts` | `warmPanelChunks()`（空闲 + 串行 + 静默） | P13 的配对项 |
| 计划任务 | `MossFrpEnsure` 改 `wscript` + 恢复 Administrator；`MossPilotAutostart` 补隐藏参数；`MossCloudflaredTunnel` 禁用 | P12 |
| VPS | nginx 443 + TLS + certbot；80 → 301；`hk.wujiaitool.cn` A 记录（灰云） | P07 |

---

## 6. 验收证据（可直接复现）

```powershell
# 1) 三个入口
curl.exe -s -o NUL -w "本机 %{http_code} %{time_total}s`n" http://127.0.0.1:8110/api/v1/health/live
curl.exe -s -o NUL -w "HK   %{http_code} %{time_total}s`n" https://hk.wujiaitool.cn/api/v1/health/live
curl.exe -s -o NUL -w "CF   %{http_code} %{time_total}s`n" https://moss.wujiaitool.cn/api/v1/health/live

# 2) 看门狗是否真在跑（退出码必须全 0；历史上 tunnel 一直是 1）
Get-ScheduledTask -TaskName 'Moss*' | Get-ScheduledTaskInfo |
  Select-Object TaskName, LastRunTime, LastTaskResult

# 3) .ps1 编码护栏（缺 BOM 的脚本会在 PS 5.1 下静默死亡）
.venv\Scripts\python.exe -m pytest tests/unit/test_ps1_encoding.py -q -p no:randomly

# 4) 页签清单三处一致性
.venv\Scripts\python.exe -m pytest tests/unit/test_nav_views_single_source.py -q -p no:randomly

# 5) 发布真相：必须打公网入口，本地快不等于线上快
.venv\Scripts\python.exe manage.py build
.venv\Scripts\python.exe manage.py ship-frontend
```

**本轮最终数字**：HK 直连 **30/30、中位 0.93s、最差 1.85s**；
CF 强制 IPv4 24/30、最差 20.44s；CF 默认中位 2.88s；
首屏入口 JS 85.0 KB(gzip) / 1.15s；本机后端 2~3 ms；
全量测试 4972 passed；`.ps1` 护栏 24 passed；页签一致性 13 passed。

---

## 7. 追加（23:00 复查时发现）：对外中断 P15 ——「值守把自己的后端判死了」

> 这一条是**在核对本文档 §6 验收证据时当场发现的**：文档里写着"三个入口 200"，
> 实际打过去是 **本机 000 / HK 502 / CF 502**。它同时补上了 §4 里那个
> "隧道与 frpc 一起消失的真凶未定位"所对应的**同一类**根因。

### P15 症状

| 观测 | 值 |
|---|---|
| 本机 `127.0.0.1:8110/api/v1/health/live` | **000**（2.0 s 无响应；不是 refused） |
| HK / CF 入口 | **502**（nginx 与 cloudflared 都连不上本机 8110） |
| 隧道侧 | **完全正常**：Cloudflared 服务 Running、cloudflared ×1、frpc ×1、17000 在听 |
| `backend.log` | 连着多次 `Application startup complete` + `Uvicorn running on http://127.0.0.1:8110` |
| `backend_incidents.jsonl` | 150 秒内 **6 次** `restart`，理由全是"端口无监听（进程已消失）"，`in_job: none`、内存 59% |

### 定位：一次隔离实验把凶手从"外部"改成"自愈"

```
禁用 MossPilotWatchdog → 前台跑一个实例 →
t+15s … t+120s  HTTP 全程 200，事故流水 40 → 40（零新增）
```

**结论：后端本身完全健康，杀死它的是"自愈"本身。**

关键反证在 `backend.log`：**三个实例都打印了 `Uvicorn running on 127.0.0.1:8110`**
（PID 7212 / 10576 / 12712 …）。在 Linux 上第二个必然 `address already in use` ——
**Windows 允许重复绑定同一个 LISTEN 端口**，与 P10 是同一个平台语义坑，
只不过这次踩它的是后端而不是隧道。

### 根因：存活判据是一次 **0.4 秒**的 TCP connect

```python
def port_open(host, port, timeout=0.4):          # ← 0.4 秒
    return s.connect_ex((target, port)) == 0

def diagnose_port(port):
    if not port_open("127.0.0.1", port):
        return {"occupied": False, ...}          # ← 0.4 秒连不上 = "端口没人听"
```

`cmd_ensure` 的判据是"端口有没有人听"——方向是对的（它明确拒绝拿 `/health`
当存活判据，理由写在 docstring 里，也有测试钉住）。**问题在这个判据的实现**：
它把"0.4 秒内没应答"等同于"进程已消失"。

于是形成**正反馈**：

```
后端忙 >0.4s（冷启动 / 进程内调度跑数据作业 / 多实例争 SQLite 单写者）
  → port_open 超时 → diagnose_port 判"occupied: False"
  → ensure 认为"进程已消失" → 再起一个实例
  → Windows 允许它照样绑定成功 → 两个实例争单写者 → 更慢
  → 下一次探测更容易超时 → 继续复制
```

时间戳完全吻合（两次事件只隔 6 秒，而调度间隔是 60 秒，说明是**一次调用内的重试循环**）：

```
22:57:16 restart_ok   →  22:57:22 端口无监听（6 秒）
22:57:26 restart_ok   →  22:58:08 端口无监听（42 秒）
```

**为什么这条最值钱**：它一次性解释了长期悬案的全部异常特征 ——
`in_job: none`、无崩溃日志、不走 lifespan、端口"无监听"但进程还在。
真相是**没有任何凶手，也没有任何进程被杀**：后端只是有半秒没应答，
就被自己的值守判定为死亡并复制了一份。

### 本次处置（已完成）

1. `manage.py stop` 清零（确认残留 uvicorn = 0、8110 无监听）；
2. 用 **`Win32_Process.Create`**（父进程 = `WmiPrvSE`，**不在任何 Job Object 里**）
   重新安置——
   `"<.venv\python.exe>" manage.py start --env pilot --port 8110 --daemon`；
   注意 `manage.py` 的守护标志是 `CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW`，
   **没有 `CREATE_BREAKAWAY_FROM_JOB`**（第 420 行），所以**谁启动它决定了它活多久**：
   计划任务/工具调用的 Job 一关，子树就被收走；
3. 恢复 `MossPilotWatchdog`，盯 **180 秒**（跨 ≥2 次 `PT1M` 触发）：
   `LastTaskResult=0`、uvicorn 恒为 2（启动器+工作进程 = 一个实例的正常形态）、
   **事故流水 40 → 40 零新增**、三入口 200（本机 2 ms / HK 0.92 s / CF 1.20 s）。

### 已修（本次，23:1x）

判据顺序倒过来了 —— **先问"netstat 里有没有人持有 LISTEN 套接字"，再问"答不答"**：

| | 改前 | 改后 |
|---|---|---|
| 主判据 | `port_open(timeout=0.4)` 的**一次** connect | `find_listening_pid()`（LISTEN 套接字） |
| connect 的角色 | **第一道门**（失败即判"空闲"） | 复核手段，且要求**连续 3 次 × 1 秒**都失败 |

进程持有 LISTEN 就是活着，与它几毫秒内答不答无关 ⇒ `ensure` 不可能再因为
"后端忙了一下"而复制实例 —— 而复制正是 P15 里把可用性问题变成自造故障的那一步。

另一个刻意的选择：**netstat 看不到 LISTEN、但 connect 能连上**是最矛盾的情形
（netstat 解析失败 / 端口被识别不出归属的东西持有）。此时按 **"有人在"** 处理
（`occupied: True` + `is_ours: False`），让 `ensure` 走"被别的程序占用"那条分支
**拒绝启动**并记账。宁可让值守报出来让人看，也绝不在可能已有实例时再复制一个。

**验证**：

- 新增 4 条回归，其中最关键的一条是 P15 的**直接反例**：
  "connect 永远失败，但有 LISTEN 套接字 → 必须判**活着**"；
  `test_manage_ensure.py` + `test_manage_no_console_window.py` 共 **19 passed**；
- 真实场景 no-op：`✅ 8110 上本项目实例运行中（PID=20016），无需处理。`
  —— 退出码 0、事故流水 40 → 40、uvicorn 仍为 2（一个实例，零复制）；
- `netstat` 实测匹配：`TCP 127.0.0.1:8110 … LISTENING 20016`。

### 可选的进一步加固（未做）

1. **给 `ensure` 加重启速率限制**：例如"10 分钟内最多重启 1 次，超限记
   `restart_throttled` 并拒绝"。判据修好之后它不再必需，但它是"误判无法自持"
   的最后一道保险。
2. `pilot_watchdog.ps1` 的退出码语义可再收一道：把"一分钟内连续两次 `端口无监听`"
   当作需要人来看的信号，而不是继续重启。

⚠️ **`PT1M` 是 21:34~21:44 那轮改动的有意设计**（`tests/unit/test_manage_ensure.py`
的注释已同步为"每分钟一次"），**不要擅自改回 5 分钟** —— 本次改的是判据，不是频率。

### 复现与验收命令

```powershell
# 1) 是不是"忙 >0.4s 就被判死"：看事故流水的重启间隔与 in_job
Get-Content data\run\backend_incidents.jsonl -Tail 8

# 2) 是不是重复绑定了：同一个 8110 上出现多组 uvicorn（Windows 允许）
Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
  Where-Object { $_.CommandLine -match 'uvicorn' } | Select-Object ProcessId,ParentProcessId,CreationDate

# 3) 四组数字（本机 / HK / CF / 看门狗退出码）必须同时成立才算修好
curl.exe -s -o NUL --noproxy '*' -w "本机 %{http_code} %{time_total}s`n" http://127.0.0.1:8110/api/v1/health/live
curl.exe -s -o NUL -w "HK   %{http_code} %{time_total}s`n" https://hk.wujiaitool.cn/api/v1/health/live
curl.exe -s -o NUL -w "CF   %{http_code} %{time_total}s`n" https://moss.wujiaitool.cn/api/v1/health/live
Get-ScheduledTask -TaskName 'Moss*' | Get-ScheduledTaskInfo | Select-Object TaskName,LastTaskResult
```
