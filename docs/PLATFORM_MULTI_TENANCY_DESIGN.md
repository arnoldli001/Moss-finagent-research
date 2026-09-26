# 平台多租户与权限设计（Platform Tenancy & Access Control）

> **文档定位**：本文是 [`MULTI_TENANCY_DESIGN.md`](MULTI_TENANCY_DESIGN.md) 的**平台级上位文档**。
> 那篇讲的是"为什么这样设计"（合规视角、四眼原则、审计链），本文讲的是
> **"全平台 10 个模块怎么改才能远程多用户共享，且个性化数据各存各的"**——
> 含资源归属矩阵、权限码表、数据模型 DDL、逐模块改造清单、部署演进与验收标准。
>
> **目标读者**：① 实现者（你自己）——每一条都写成"可直接开工"的粒度：
> 文件路径 + 表名 + 字段 + 现有代码接入点；
> ② **面试官**（量化开发 / AI 应用开发）——**附录 C 是专门的面试防御包**：
> STAR 叙事、13 个尖锐追问的应答、双岗讲法差异、按实现进度分级可写的简历条目。
>
> **状态**：设计稿（待评审）+ 面试准备。**落地顺序**见 §10，**不要跳步**。
>
> **数字纪律**：全文区分 `实测`（本仓库可复现）/ `目标`（设计值）/ `设计推演`（未验证分析）。
> **三者不许混说**——与 [`RESUME_PROJECT.md`](RESUME_PROJECT.md) §D 的自我约束一致。
>
> **免责声明**：本系统输出仅供技术研究，不构成投资建议。

---

## 一、结论先行（TL;DR）

| 问题 | 结论 |
|---|---|
| 需不需要"多实例"？ | **不需要按用户分实例**。需要的是把**进程内全局状态**搬走 + 把**重活隔离到独立进程**。 |
| 怎么做隔离？ | 三级资源模型：**全局共享（行情/因子/模型）→ 租户共享（策略库/板块组）→ 用户私有（自选/权重/提醒设置）**。 |
| 权限怎么控？ | 在现有 `authorize(action, resource)` 上扩展**两级**：① 能力码 `<module>.<object>.<action>` 按模块授予；② 行级 `user_id` 维度（现在只有 `tenant_id`）。 |
| 现有底座能用吗？ | **能，而且相当好**。`Principal` / `authorize()` / `DataClass` / `WallGroup` / `tenant_column_guard()` / `postgres_rls_ddl()` 都在，缺的是"**用户**这一级"和"**能力码**这一级"。 |
| 工作量最大的是什么？ | 不是权限，是 **`src/intraday/service.py` 的 30 个进程内缓存字段**（§6.1）与 **31GB 行情仓库的多进程共享**（§8.3）。 |
| 一句话路线 | 先"**用户维度贯通**"（正确性），再"**共享计算层**"（性能），最后"**进程拆分+多副本**"（容量）。 |

> **面试视角的一句话**：本文最值钱的部分不是"我要建什么"，而是
> **"我实测发现单实例饱和在 5~6 req/s，但整机 CPU 只用了 6~13%，
> 于是判定瓶颈是重复计算而不是核数，并据此否掉了自己最初的加实例方案"**。
> 这个判断过程（§2.2 H3 + §7.1 + 附录 C.1）比任何"高并发架构"都更难被追问穿。

---

## 二、现状盘点（全部有据可查）

### 2.1 已有资产（不要重造）

| 资产 | 位置 | 现成能力 | 缺什么 |
|---|---|---|---|
| 身份快照 | `src/core/tenancy.py:92` `Principal` | `user_id` / `tenant_id` / `roles` / `clearance` / `wall_group` / `groups`，不可变 | 无"租户成员关系"来源（token 带什么就是什么） |
| 身份注入 | `src/api/tenancy_middleware.py:204` | Bearer → 派生 Principal → contextvar → 审计埋点；默认拒绝 | **未强制**（`MOSS_TENANCY_ENFORCE` 未开 = 全员 `local-dev`） |
| 令牌 | `tenancy_middleware.py:134` | HMAC 签名载荷（服务端派生、不可伪造） | **无过期、无吊销、无刷新**；前端不发 `Authorization` 头 |
| 授权引擎 | `src/core/policy.py:160` `authorize()` | RBAC 动作矩阵 + ABAC 数据分级 + 隔离墙 + 四眼标注；默认拒绝 | 动作只有 **13 个粗粒度**，**没有模块维度** |
| 行级隔离（应用层） | `src/infrastructure/security/rls.py:65` `tenant_column_guard()` | 查询期下推；未登记表**直接抛错** | `TENANT_COLUMNS` 只登记了 **7 张假想表**（`watchlist`/`strategy`…），与真实表名不符；**无 `user_id` 维度** |
| 行级隔离（DB 层） | `rls.py:156` `postgres_rls_ddl()` | 生成 `ENABLE`+`FORCE ROW LEVEL SECURITY`+`CREATE POLICY` | DDL 针对上面那 7 张不存在的表；**从未执行过**（当前后端是 SQLite） |
| 审计 | `TenantAuditLog`（JSONL+哈希链）；`src/infrastructure/repositories/audit_chain.py` | 追加写、可验证、埋点在中间件 | 无法分页/检索；**无租户列** |
| 缓存隔离工具 | `tenancy.py:208` `tenant_cache_key()` | 按租户（敏感内容再加清关等级）分区 | **生产代码里零调用**（仅单测用到）；且**不含用户维度** |
| 服务端系统上下文 | `tenancy.py:198` `system_scope(reason)` | 后台任务显式降权身份，`reason` 进审计 | **生产代码里零调用**；定时任务与后台循环都没用它 |

### 2.2 硬伤（必须改，否则"远程多用户"无从谈起）

**H1 · `tenant_id` 之外没有"用户"这一级。**
`TENANT_COLUMNS` 的设计意图是"租户 = 部门，部门内共享"。但需求是
**每个人的自选池和个股参数都不一样** —— 这是**用户级**隔离，比租户级更细一档。
现有代码里连一处 `user_id` 行级过滤都没有（`grep user_id src/intraday` → 0 命中）。

**H2 · 全局可变状态散布在进程内与磁盘上。**

| 类型 | 实例 | 多用户后果 |
|---|---|---|
| 进程内缓存 | `IntradayService` **30 个** `self._*cache/_lock/_state` 字段；`fundflow/service.py` 7 个；`intraday/board.py` 7 个；`quant/concept_repo.py` 6 个 | 缓存键不含用户 → **串号**；多副本 → 各自一份、命中率归零 |
| 前端可写的 YAML | `configs/intraday.yaml`（自选池 + `overrides` + `pinned`，`intraday/config.py:1163` 原子替换写盘） | 所有人共用一份自选；A 加票 → 全表缓存作废 → 所有人的列表重算 |
| 全局 JSON 状态 | `intraday/hot_cache.py`（自选概览快照）、`auction_select/ecology_cache.py`、`auction_select/preheat_cache.py`、`quant/*/*.json`（选股结果/清单） | 同上 |
| DB 表（仅 `code` 主键） | `dim_intraday_profile`：`code TEXT PRIMARY KEY`（`intraday_profile_sqlite_repo.py:38`，实测 2 行） | **一只票全系统一份权重** |
| DB 表（选择列表） | `dim_fund_flow_watch`(36)、`sector_crowding_watch`(7)、`auction_watch`(4) | 全平台共用一份"我关注的" |

**H3 · 计算与数据获取没有共享，"每人各算一遍"。**
压测实测（`scripts/_probe_concurrency.py`，服务在跑、只读接口）：

| 场景 | p50 延迟 | 吞吐 |
|---|---:|---:|
| 单发（暖缓存） | 0.27~0.39s | — |
| **同一只票** 并发 5 | 0.95s | 5.3/s |
| **同一只票** 并发 50 | 6.54s | 4.8/s |
| 不同票 并发 10 | 11.35s | 0.54/s |
| 自选列表 并发 60（**走缓存**） | 0.05s | **245/s** |

- `WS /ws/intraday` 在 `intraday.py:396` **每个连接各跑一次 `service.snapshot(target)`**，
  无单飞、无结果共享 → 100 人看同一只票 = 算 100 遍。
- 全流程同步 pandas（`build_intraday_features` / `replay_totals` / `run_engine`，
  `service.py:920-1062`）**跑在事件循环里**，没有 `asyncio.to_thread` 包裹 → 单请求阻塞全服务。
- 结论：**吞吐饱和在 5~6 请求/秒**。100 人正常音量（100 路 WS / 15s + 45 只票 / 60s 重算）≈ 7.5/s。

**H4 · 重任务与实时链路抢资源。**
`quant/quant_select_service.py`（全市场选股，实测单次 27s、撞预热时 828s）、
`mainline` 回测、`sector_crowding` refresh（遍历 3.5 万分区目录，实测 140s）、
`data_health`（6~28s I/O）全部跑在 **API 进程内**。多用户下"一个人点刷新，所有人卡"。

**H5 · 定时任务假设单进程。**
`src/scheduler/service.py` 进程内 `CronScheduler`（`api/main.py:248`），注册表含
`quant_data_sync`（16:40）、`intraday_t_scan`（每 5 分钟）、竞价调度（09:15/09:25）等。
**N 个副本 = N 倍全市场写入**（`data/quant` 已 31.26GB）。

**H6 · 只能本机访问。**
`uvicorn src.api.main:app --host 127.0.0.1 --port 8100`（实测 cmdline），
无 TLS、无反代、无 CORS 策略、无速率限制。

### 2.3 同类实现对照：`D:\code\quant\quantCode\QuantMind`（**本机就有一套可参考的**）

本机 `D:\code\quant\quantCode\QuantMind` 是一份**完整的多用户量化平台**
（FastAPI + Electron/React，已支持服务端部署）。它的做法可直接对照借鉴：

| 维度 | QuantMind 的做法 | 对本项目的取舍 |
|---|---|---|
| 租户轴 | `users` 表有 `tenant_id`，唯一约束是 **(tenant_id, username)** / (tenant_id, email) / (tenant_id, phone) —— **租户内唯一、跨租户可重名** | ✅ 直接采纳（本项目 `dim_user` 应加同样约束） |
| 权限模型 | `roles` / `permissions` / `user_roles` / `role_permissions` 四表，权限码 `<resource>.<action>`（`order.create`、`system.audit`）；角色带 `priority` | ✅ 采纳"四表 + 码表"；但**不采纳两段扁平码**——本平台模块多、层级深，需 `<module>.<object>.<action>` 三段 |
| 授权落点 | FastAPI 依赖注入 `@require_permission("order.create")`，逐端点声明 | ✅ 采纳"逐端点声明"的显式性；落点仍在 `authorize()` 单一入口（§11 D3） |
| 权限缓存 | `user:permissions:{user_id}` 缓存 10 分钟，改角色时失效 | ✅ 采纳；**补一条**：变更必须能**主动失效**（Redis 广播 + 权限版本号），否则"刚收权还能用 10 分钟"是审计事故（§8.5.2） |
| 个人资源隔离 | `check_user_permission(user_id, resource_user_id)` 只允许访问自己的资源；用户策略按 user 分目录 | ✅ 思路一致；本项目升级为**表级 `user_id` + 数据库 RLS 双层**（它只有应用层一层） |
| 服务间身份 | 内部头 `X-Internal-Call: <secret>`，**且网关负责剥离外部传入的这几个头** | ✅ 采纳这条关键细节（本项目拆进程时同样需要"内部头只在网内可信"） |
| 行情/因子共享 | 48 维特征由**外部服务**写入 `market_data_daily`，各服务只读 | ✅ 与 §3.2"L0 因子层与用户无关"完全一致，**独立印证** |
| 进程拆分 | api(8000) / engine(8001) / trade(8002) / stream(8003)，Redis 按 DB 编号分区 | ✅ 参考其"按职责拆"而非"按用户拆"；§8.2 拆成 api/realtime/worker/scheduler 四类 |
| 用户档案 | `user_profiles` 存 `risk_tolerance` / `preferences`(JSON) / `notification_settings`(JSON) | ✅ 采纳：本项目个人设置可合并进一张 `dim_user_pref`，减少表数 |

**不要照抄的地方**：
1. 它是**面向公众/多公司**的商业平台（租户 = 客户公司），本项目是**机构内部平台**
   （租户 = 部门/策略组）→ 它防"客户 A 看客户 B"，本项目更该防"研究员看别人的在研策略"
   （`MULTI_TENANCY_DESIGN.md` §零 已讲清，**以它为准**）。
2. 它有 `is_admin` 布尔字段 + 角色双轨判定，本项目**不用布尔超管**：
   用 `role` + 能力码，避免"超管绕过一切"（`policy.py` 默认拒绝原则）。
3. 它只有应用层 `user_id == resource_user_id` 单层校验，本项目要求**应用层下推 + RLS 双保险**。

### 2.4 参考项目：本机另一份姐妹项目 `moss-finance-assistant`（**直接借鉴**）

需求方指明可参考 `D:\code\moss-finance-assistant`（同为 FastAPI + 多 Agent，面向散户，
MIT）。它的**工程化底座比本项目成熟**，以下四处**直接借鉴**（不是照抄代码，是采纳其结论）：

| 借鉴点 | 它的做法（位置） | 本项目怎么用 |
|---|---|---|
| **Actor 模式做状态隔离** | `shared/actors/actor_base.py`：邮箱 + 串行消费 + `next_state = f(state, input)` 纯函数边界；`circuit_breaker_actor.py` 把熔断状态全收进一个 Actor | §7.6.2：把 `SourceHealthTracker`、`_watch_cache` 等**有竞态的状态** Actor 化 |
| **熔断器 + SLO 目标** | `config/constants.py`：`CB_*_FAILURE_THRESHOLD=3`、窗口、恢复冷却；`SLO_AVAILABILITY_TARGET=0.99`、`SLO_LATENCY_P95_SEC=30`、`SLO_MAX_TASK_SEC`、error budget 窗口 | §8.8.2 的容灾分级按 RTO 排；§9.1 的指标加 SLO 口径 |
| **RBAC 策略表 + 每角色限流** | `config/rbac_policy.json`：每角色带 `permissions` + **`rate_limit_per_min`** + **`max_rows_per_query`** + `allowed_endpoints`，**60 秒热加载** | §4.6 的 `dim_tier` 把**配额与限流和权限写在同一条配置**里；moss 已验证这种"权限+限流同表"的形式好用 |
| **强身份隔离（原子级）** | `shared/utils/auth.py`：`CurrentUser(user_id, role)` + **`current_user_id_must_match()`** —— 非 owner/admin 一律只能操作自己的数据 | §8.6 的注册审批 + §5.4 的用户级 RLS，是同一目标的两个层次 |

**它的短板（本项目不要学）**：
① 它**没有注册审批流**（`grep pending/approv` 在业务代码里无命中）——本项目的 §8.6 是新增；
② 它用 `role` 字符串 + JSON 策略做授权，**没有数据分级（ABAC）与隔离墙**维度，
   因为它是"散户工具"不是"机构隔离系统"；本项目两维都保留（§4.1）。

### 2.5 模块与接口现状（用于权限码设计）
前端顶层页签（`web/src/App.tsx:161`）：`投研分析 / 调度管理 / 运行指标 / 策略回测 /
量化交易 / 主线挖掘 / 资金流监控 / 事件告警`。
「量化交易」是容器页签（`QuantTabContainer.tsx`），内含
`量化选股 / 竞价选股 / 做T辅助` 三个**同级模块**。
「资金流监控」内含 `资金流` + `板块拥挤度`（`FundFlowPanel.tsx:76`）。

后端 15 个路由模块、**约 130 个端点**（已全量枚举）。
其中约 50 个是"写用户状态"的端点（自选/权重/提醒/策略），这是改造重点。

数据库实测 **33 张表**；`dim_intraday_profile` 只有 2 行、`sector_crowding_watch` 7 行——
说明"个人数据"体量极小（百人量级 < 1 万行），**用户维度落库的成本几乎为零**。

### 2.6 监管依据（**条款级，已核对一手文本**）

**（1）法律层**

| 依据 | 原文要点 | 本文落点 |
|---|---|---|
| 《证券法》**第五十四条** | 禁止从业人员"**利用因职务便利获取的内幕信息以外的其他未公开信息**，违反规定，从事与该信息相关的证券交易活动，或者**明示、暗示他人**从事相关交易活动"（[全文](https://www.beijing.gov.cn/zhengce/zhengcefagui/qtwj/202111/t20211124_2544351.html)） | "信息隔离墙"在国内法上的根：`WallGroup` + `WallCrossing` 的可追溯设计（§4.1） |
| 《证券法》**第一百九十一条第二款** | 依内幕交易条款处罚（没收违法所得并处 1~10 倍罚款；无违法所得或不足 50 万元的处 50~500 万元） | 说明这不是"最佳实践"而是**罚则支撑的强制要求** |
| 《证券法》**第二百一十四条** | 对未按规定保存文件资料、**篡改毁损资料**设罚则 | "审计日志不可篡改"的直接法律落点 → §5.3 哈希链 |
| 《网络安全法》**第二十一条第（三）项** | **留存相关的网络日志不少于六个月**（[全文](https://www.qstheory.cn/2019-09/09/c_1124977535_2.htm)） | `fact_access_audit` 的保留期下限。**这是"6 个月"的唯一可引依据** |

**（2）部门规章：⚠️ 名称必须写对**

正式名称是《**证券基金**经营机构信息技术管理办法》（证监会令第 152 号，2019-06-01 施行，
经证监会令第 179 号 2021 修订）——**不是**"证券期货经营机构"
（[国务院公报版](https://www.gov.cn/gongbao/content/2019/content_5383798.htm)、
[国家规章库修订版](https://www.gov.cn/zhengce/2021-12/16/content_5724549.htm)）。

| 条款 | 要求 | 本文落点 |
|---|---|---|
| **第三十二条** | 遵循**最少功能以及最小权限**原则分配权限，**并履行审批流程**；建立权限的**定期检查与核对**机制；对重要信息系统实施**开发、测试、运维分离**，保证**岗位相互制衡** | §4.3 能力码默认拒绝 + 授权审批留痕；§5.3 定期核对。**"双人复核"的强制依据在这一条**（措辞是"岗位相互制衡"，规章里没有"双人复核"四个字） |
| **第三十条** | 数据按重要性、敏感性**分类分级**并作差异化制度安排 | §4.1 的 `DataClass` 四级制 |
| **第三十一条** | 网络隔离、用户认证、访问控制、数据加密、日志记录 | §8.1 网络层 + §5.4 RLS |
| **第二十八条** | 重要信息系统应具备**可审计功能** | §5.3 可检索审计表 |
| **第二十五条** | 建立与重要程度相适应的**日志留痕机制**（**注意：未规定月数**） | 保留期以《网络安全法》6 个月为下限 |
| **第十六条** | **审计报告保存期限不得少于二十年**；专项审计不低于每年一次 | 审计**报告**（非原始日志）的 20 年留存要求 |
| **第二十一条** | 须有独立于生产环境的开发测试环境；测试环境用未脱敏数据须采取与生产环境**同等**安全控制 | 与 `src/core/redaction.py` 写侧脱敏一致 |
| **第三十九条** | 应急演练报告保存期限不得少于**五年** | 运维记录留存 |

**（3）自律规则：《证券公司信息隔离墙制度指引》**（中证协发〔2010〕203 号，
2011-01-01 施行，[全文](https://law.esnai.cn/mview/93330)）——**逐条可映射为系统需求**

| 条款 | 要求 | 系统映射 |
|---|---|---|
| 第八条 | **需知原则**：敏感信息仅限有合理业务需求或管理职责需要者知悉 | "默认拒绝"的合规依据（`policy.py` 核心原则） |
| **第十三条** | 冲突业务的**信息系统相互独立或实现逻辑隔离** | **"控制台隔离"的监管依据** → §9.4 |
| 第十五~十七条 | 跨墙须**审批**；建立跨墙人员行为规范；跨墙结束且信息已公开或不再重大影响后方可**回墙**；合规部门记录并监控 | 跨墙＝**临时提权**，需申请-审批-生效-**到期回收**全生命周期留痕（不是布尔值） |
| 第十九~二十二条 | **观察名单**（业务照常但受监控）与**限制名单**（限制相关业务活动）；**高度保密** | **证券维度**的第五正交轴 → §4.4 |
| 第十二条 | 同一高管原则上不同时分管冲突业务；决策机构**回避制度** | 角色互斥（SoD）→ §4.3.1 |
| 第二十六~二十八条 | 研报发布前审查；禁止发布前向研究对象提供含评级/目标价章节；研究考核不与投行业绩挂钩 | 研究结论的 **发布态 vs 在研态** 必须可区分（§3.3 `visibility`） |

**（4）通用控制定义**：四眼原则 / dual control 的国际通用定义是
**NIST SP 800-53 AC-5 (Separation of Duties)**，其讨论段明确
"**管理访问控制的安全人员不得同时管理审计职能**"
（[AC-5](https://www.stigviewer.com/controls/nist-800-53/AC-5)）。
→ 这正好支撑本项目一个既有取舍：**`platform.audit.read` 对 admin 默认拒绝**（附录 A）。

#### ⚠️ 本节易错点（写错就是硬伤，面试官里可能有合规岗背景）

| 易错说法 | 正确说法 |
|---|---|
| ❌《**证券期货**经营机构信息技术管理办法》 | ✅《**证券基金**经营机构信息技术管理办法》（证监会令第 152 号） |
| ❌ "等保 2.0 要求日志留存 6 个月" | ✅ 6 个月出自《**网络安全法**》**第二十一条**。等保 2.0（GB/T 22239-2019）为付费标准，本文未取得可公开引用的正文，**不引用** |
| ❌ "规章要求双人复核" | ✅ 证券期货规章**没有"双人复核"措辞**；强制依据是第三十二条"**岗位相互制衡/开发测试运维分离**"，通用定义引 NIST SP 800-53 AC-5 |
| ❌ "审计日志留存 20 年" | ✅ 第十六条是"**审计报告**保存 ≥ 20 年"；**原始日志**下限是《网络安全法》的 6 个月。两者不是一回事 |

**（5）RLS 的已知陷阱（PostgreSQL 官方文档 + 公开案例）**

- **无策略即默认拒绝**；permissive 用 `OR` 合并、restrictive 用 `AND` 合并。
- **`TRUNCATE` 与 `REFERENCES`（外键）不受 RLS 约束**；**参照完整性检查永远绕过 RLS**
  —— 官方明确警告这会形成**隐蔽信道**。
- **superuser 与 `BYPASSRLS` 角色永远绕过**；**表 owner 默认也绕过** →
  **必须 `ALTER TABLE ... FORCE ROW LEVEL SECURITY`**；"只 ENABLE 不 FORCE"是最常见事故源。
- 策略表达式里做子查询有**竞态**（READ COMMITTED 下可读到旧权限快照）。
- **连接池复用导致租户上下文泄漏**是已记录问题
  （[公开案例](https://stackoverflow.com/questions/80003232/postgres-row-level-security-leaks-across-tenants-when-using-a-pooled-connection)）：
  标准修法是**事务级** `SELECT set_config('app.tenant_id', $1, true)`
  （第三参 `is_local=true`）在**显式事务内**执行；裸 `SET`（会话级）在连接池回收后**仍留值**
  （[模式记录](https://raw.githubusercontent.com/Nelson-Lamounier/ai-applications/refs/heads/develop/docs/patterns/per-transaction-rls.md)）。
- 官方依据：[PostgreSQL 5.9 Row Security Policies](https://www.postgresql.org/docs/current/ddl-rowsecurity.html)。

### 2.7 业界做法对标（三家可查证的实现）

> 全部有官方文档来源；**未找到权威来源的（聚宽 / 优矿 / 掘金 / vn.py / WorldQuant BRAIN）
> 明确不引用**，以免在面试里被问住。

**（1）隔离模式怎么选：有规模判据**

| 模式 | 适用规模 | 成本 | 迁移复杂度 | 隔离强度 |
|---|---|---|---|---|
| 共享表 + `tenant_id` | **上千租户** | 最低 | 最低（DL 全成/全回滚） | 最弱（靠应用 + RLS） |
| 每租户 schema | 数十~数百 | 中 | 中 | 中 |
| 每租户独立库 | **5~50 租户** | 最高 | 最高（需自建迁移编排） | 最强 |

- 依据：Citus（Microsoft）（[Designing SaaS](https://learn.microsoft.com/en-us/postgresql/citus/designing-saas)）、
  AWS silo/bridge/pool 与"真实系统多为 bridge"
  （[分区模型](https://docs.aws.amazon.com/prescriptive-guidance/latest/saas-multitenant-managed-postgresql/partitioning-models.html)、
  [Bridge model](https://docs.aws.amazon.com/wellarchitected/latest/saas-lens/bridge-model.html)）。
- **对本项目的用处**：我们是"10 租户 × 100 用户"，落在"5~50 用独立库"的区间**边缘**，
  但**数据分层后结论反转** —— 行情/因子（31GB，共享价值最高）必须共享，
  用户私有数据（< 1 万行）独立与否无所谓。故 **ADR D1 的"共享表 + RLS"成立**，且**有判据支撑**。

**（2）权限模型：ReBAC 的边界与"撤权延迟"的正解**

- **Zanzibar**（USENIX ATC'19）：① 关系元组 `object#relation@user`，user 可为 **userset**
  → 组中组/目录继承天然可表达；② userset rewrite rules（union / computed_userset / tuple_to_userset）；
  ③ API = **Check / Read / Write / Expand / Watch**；④ **new-enemy problem**：撤权后仍读到旧
  ACL 快照，解法 **zookie**；⑤ 规模：2 万亿+ 元组、峰值 ~1000 万 QPS、p50 ~3ms
  （[论文](https://www.usenix.org/conference/atc19/presentation/pang)）。
- **OpenFGA 官方多租户模式**：建 `organization` 类型，其余资源 `parent` 指向它 →
  **默认硬隔离**；跨租户分享再加个体元组；全租户共用一个 store
  （[Multi-Tenant SaaS](https://openfga.dev/docs/use-cases/multi-tenant-saas)）。
- **对本项目的用处**：ADR D2 说"不引入 ReBAC"，现在**补一条边界**：
  我们的资源确实是树形归属，**ReBAC 本可自然表达**；不引入是因为"层级只有三级且固定"。
  但 §8.5.2 第 3 条（权限缓存跨副本不一致）**正解就是 zookie 的思想**：
  **不要让授权判定依赖"缓存是否过期"，而要让它依赖 ACL 版本号**。

**（3）成熟量化平台的三层组织（直接印证 §3 的资源模型）**

| 平台 | 可查证做法 | 对本项目的印证 |
|---|---|---|
| **米筐 RiceQuant** | 策略分享**三档**：`可运行回测`（可调参、跑回测，**无代码查看权**）/ `可见代码` / `可编辑`（[官方文档](https://www.ricequant.com/doc/quant/strategy-cooperation.html)） | §3.3 的**"显式共享"三档语义**：`visibility` 不该是布尔值，而是**能力档位**（runner / viewer / editor） |
| **QuantConnect** | Organization + Tier；**算力资源（回测/研究节点）挂组织而非个人**，移除节点需 billing 权限；Object Store 为组织级共享（[Tier](https://www.quantconnect.com/docs/v2/cloud-platform/organizations/tier-features)、[Resources](https://www.quantconnect.com/docs/v2/cloud-platform/organizations/resources)） | **"配额归属组织、由管理员调配"** —— 对应 §7.2 的 `quota_json` 与 §8.5.4 容量推演；回测节点 = 重任务队列**并发上限** |
| （归纳） | 公共数据**单一副本 + 版本化 dataset id**；社区策略通过**授权元组而非复制**分享；**用户私有参数与"代码"分开存放**，共享策略时参数各归其主 | 与 §3.2 铁律"**计算可共享、结论可共享、参数不能共享**"完全一致 —— **独立印证** |

> **三段式配额隔离**（业界通行，用于 §7.3）：`(tenant, user)` 并发上限
> + 每任务 CPU/内存/时长限额 + 公平调度队列。

**（4）审计不可篡改的两条成熟路线**

- **append-only Merkle 日志**：[RFC 6962（Certificate Transparency）](https://datatracker.ietf.org/doc/html/rfc6962)。
- **WORM / 锁定保留桶**：写入后保留期内不可删除改写。
- **务实组合（本项目取法）**：**哈希链 + 定期锚定** —— 每条记录
  `hash(前一条 hash ‖ 本条内容)`（`audit_chain.py` 已实现），链头**每日锚定**
  到 WORM 桶或外部时间戳服务。既能**检测篡改**，又不必全量用昂贵存储。
  回应《证券法》第二百一十四条与《证券基金经营机构信息办法》第十六条、第二十八条。

**（5）服务账号与后台任务：不持有用户身份**

- 原则："**消灭长期凭证、改用短期凭证**"
  （[AWS Well-Architected Security Pillar](https://docs.aws.amazon.com/wellarchitected/latest/security-pillar/welcome.html)）。
- 落地：① 后台任务只持 **system scope**，绝不携带用户 token（本项目已有
  `tenancy.system_scope(reason)`，但**生产代码零调用**——这是要补的接线）；
  ② 确需代表用户执行时，由调度器下发**一次性 impersonation token**（短 TTL、窄权限、
  绑定 `tenant_id`+`user_id`+任务 id）；
  ③ **system 身份也走 RLS**（显式 `SET LOCAL app.tenant_id`），**不用 `BYPASSRLS`**。
- 高权操作形态：**break-glass 临时提权**（限时 + 双人批准 + 自动到期 + 全链路留痕），
  申请与批准**做成两个 API / 两张表**，DB 层禁止自批。

---

## 三、资源归属模型（三级 + 一条铁律）

### 3.1 三级模型

```
┌─ L0 全局共享（platform）───────────────────────────────────────────┐
│ 行情仓库存量、分钟/日线、因子面板、概念板块、涨停池、模型文件、       │
│ 平台默认模板（权重模板、选股策略预设、板块清单）                      │
│ 归属：tenant_id = NULL，user_id = NULL                             │
│ 权限：只读；写只允许 platform_admin，且走四眼                        │
│ 缓存：进程内 + Redis，**键不含用户**（这就是它的价值：算一次给所有人）│
└──────────────────────────────────────────────────────────────────┘
                              ↑ 读取
┌─ L1 租户共享（tenant）─────────────────────────────────────────────┐
│ 部门策略库、共享板块组、租户级默认自选池、租户审计视图、配额配置      │
│ 归属：tenant_id = 'dept-x'，user_id = NULL                        │
│ 权限：租户内按角色                                                  │
└──────────────────────────────────────────────────────────────────┘
                              ↑ 继承 + 覆盖
┌─ L2 用户私有（user）───────────────────────────────────────────────┐
│ 自选股池、个股权重档案、做T阈值/档位、提醒规则与已读态、              │
│ 个人选股策略与条件、关注列表（资金流/拥挤度/竞价）、个人看板布局      │
│ 归属：tenant_id = 'dept-x'，user_id = 'u-123'                     │
│ 权限：仅本人（+ 显式共享档位；管理员默认**不可读**）                 │
└──────────────────────────────────────────────────────────────────┘
```

### 3.2 铁律：**计算可以共享，结论可以共享，参数不能共享**

| 对象 | 是否随用户变化 | 归属 | 缓存键 |
|---|---|---|---|
| 300308 的 5 分钟 bars / 缠论 / chip / 板块快照 | ❌ 与用户无关 | L0 | `bars:300308:5m:320` |
| 300308 的**因子原始得分**（14 个因子各自的 -1..1） | ❌ 与用户无关 | L0 | `factors:300308:<trade_date>` |
| 300308 的**总分/档位/信号** | ✅ 用户权重与阈值不同 | 派生 | `score:300308:<hash(权重+阈值)>` |
| 300308 的**神经网络拟合档位** | ✅ 拟合目标含用户档位偏好 | L2 派生 | `fit:300308:<trade_date>:<hash(口径)>` |

> ⚠️ **当前 `service.py` 的 `_fit_cache` 键是 `(code, trade_date)`，不含口径**
> —— 这是多用户下第一处会出错的代码：A 先拟合，B 看到 A 的档位。
> 修复不是"每用户各拟合一遍"（100 倍成本），而是把
> **权重/阈值指纹并入缓存键**：口径相同就共享，不同才重算。

### 3.3 "共享"的三种语义（不要混）

| 语义 | 含义 | 例子 | 实现 |
|---|---|---|---|
| **读共享** | 所有人读同一份，不能改 | 行情、因子、模型 | L0 表，无租户列；只读角色 |
| **继承** | 用户私有初始值来自上级，改动即"分叉" | 新用户自选池 = 租户默认池 | copy-on-write：首次修改时物化到 L2 |
| **显式共享** | 本人把东西发给别人/部门 | 分享权重模板给同事 | `visibility` + **能力档位**（不是布尔值）：`runner / viewer / editor`（米筐三档，§2.6） |

**默认 private**。跨用户读取必须由 owner 显式设置 `visibility`，或走 `WallCrossing` 审批。

---

## 四、身份、租户与权限模型（五维正交）

### 4.1 五个正交维度（**不要合并任何一个**）

| # | 维度 | 回答什么 | 现有落点 | 需要补什么 |
|---|---|---|---|---|
| 1 | **成员关系（租户 = 套餐层级）** | 这个人属于哪个租户、什么身份 | `Principal.tenant_id`（token 带） | 无来源校验 → 加 `dim_tenant_member`，token 只带 `user_id`，租户**服务端查**；**租户语义升格为"套餐层级"**（§4.6） |
| 2 | **RBAC（角色）** | 能做什么**类**动作 | `Role` × `_POLICY` | 动作只有 13 个、无模块维度 → **加能力码**（§4.3） |
| 3 | **ABAC（等级）** | 能看**多机密**的数据 | `DataClass` vs `clearance` | 保持不动 |
| 4 | **信息隔离墙** | 能看**哪个部门**的在研内容 | `WallGroup` + `WallCrossing` | 保持不动；补**控制台隔离**（§9.4） |
| 5 | **证券名单** | **这只票**现在能不能碰 | 无 | **新增**（§4.4） |
| — | （横切）**数据归属** | 这条数据是**谁的** | 无 | **加 `user_id`**，与 `tenant_id` 并列 |

> ⚠️ **第 1 维的语义在本次需求里变了，且必须与第 2 维分开**：
> 原设计里"租户 = 部门"（横向的组织边界）；现在实际是
> **"租户 = 套餐层级"**（VIP / 试用 / 管理，纵向的服务等级）。
> **套餐层级决定"能用多少"（配额、自选上限、刷新频率、算力），
> 角色决定"能碰什么"（能力码）。** 两者混在一起会写出
> "VIP 就能改别人的权限"这类越权（§4.6 有完整辨析）。

> **为什么必须分开**：`policy.py` 的 docstring 已就 `clearance` 与 `role` 警告过——
> 把"级别"和"角色"合成一个维度必然出现"越权即升权"。第 5 维同理：
> **"他能不能看资金流"** 与 **"这只票现在能不能碰"** 是两件事。
>
> **理论来源**：RBAC/ABAC 的规范化定义见
> [NIST SP 800-162](https://csrc.nist.gov/pubs/sp/800/162/upd2/final)；
> ReBAC 处理"资源归属/分享树"（[Zanzibar](https://www.usenix.org/conference/atc19/presentation/pang)）。
> **三者不可互相替代** —— 这是五维正交的依据。

### 4.2 租户模型：一个用户一个租户（本期不做多租户用户）

`Principal.tenant_id` 是**单值**。不要为了"一个人属于多个租户"改成列表——
那会让每个仓储查询都变成 `IN (...)` + 前端需要"当前租户切换器"。

**本期决策**：`user → tenant` 多对一。若确有"外派研究员同属两个部门"，
用**账号映射**（两个 user 记录，同一自然人，审计记 `real_user_id`），不改数据模型（§11 D6）。

### 4.3 能力码（Permission Code）：`<module>.<object>.<action>`

**格式**：三段，全小写下划线分隔。示例：

```
quant.intraday.watchlist.write      做T：改自选（个人）
quant.intraday.profile.write        做T：改个股权重档案（个人）
quant.intraday.level_fit.run        做T：触发神经网络拟合（重算力）
quant.selector.run                  量化选股：跑批（重算力）
quant.selector.train                量化选股：训练模型（重算力 + 四眼）
quant.auction.watch.write           竞价选股：改关注（个人）
research.analyze.run                投研：跑分析（花钱）
research.export.data                投研：导出（四眼）
mainline.backtest.run               主线：跑回测（重算力）
mainline.data.sync                  主线：同步底层数据（写全平台）
fundflow.watch.write                资金流：改关注（个人）
crowding.config.write               拥挤度：改清单（个人/租户）
crowding.refresh.run                拥挤度：全量刷新（重算力）
alerts.settings.write               告警：改提醒设置（个人）
alerts.scan.run                     告警：触发扫描（花钱）
scheduler.job.run                   调度：手动触发（运维）
metrics.read                        运行指标：看（运维）
platform.user.manage                平台：用户与角色管理（四眼）
platform.source.manage              平台：接数据源（四眼）
platform.audit.read                 平台：读审计（合规）
compliance.symbol_list.read         合规：读观察/限制名单（高度保密）
```

**兼容策略**：不改 `src/core/policy.py` 的既有 13 个动作（会破 2500+ 单测）。
新增 `src/core/capabilities.py`：

```python
# src/core/capabilities.py （新增）
CAPABILITIES: dict[str, frozenset[Role]] = { ... }      # 能力码 → 允许角色
DEFAULT_ROLE_CAPABILITIES: dict[Role, frozenset[str]]   # 角色 → 默认能力集（可被授权表覆盖）
```

判定顺序（**在 `authorize()` 内部扩展，不新增第二套引擎**）：

```
authorize(action, resource)
  ├─ ① 动作在 _POLICY？ ─── 否 → 拒绝（保持现状）
  ├─ ② 是能力码？ ── 是 → 走 resolve_capabilities()，见下
  ├─ ③ 角色是否有该动作（现状）
  ├─ ④ 租户匹配（现状）
  ├─ ⑤ 数据等级（现状）
  ├─ ⑥ 隔离墙（现状）
  └─ ⑦ 【新增】证券名单门禁：限制名单内的标的直接拒绝写操作
```

**能力裁决（`resolve_capabilities(principal) -> frozenset[str]`）严格三层，顺序即优先级**：

```
第 1 层  角色默认        DEFAULT_ROLE_CAPABILITIES[role] 的并集（多角色取并集）
第 2 层  租户策略        租户可**收窄**自己辖内的能力（不能给超出平台策略的能力）
第 3 层  用户增量        授权表：allow 追加、**deny 剔除**
────────────────────────────────────────────────────────────────
裁决规则：**deny 永远压过 allow**（含压过第 1 层的角色默认）。
         三层跑完再判定 —— 任何一层都不能"绕过后续层的 deny"。
```

> ⚠️ **两个容易写错的地方**：
> 1. **不要写成"角色默认 ∩ …"**：多角色时角色默认必须**取并集**
>    （一个人既是 researcher 又是 risk，两边的能力都该有）。
> 2. **真正的不变式是"deny 优先"，不是"处处取交集"**：
>    "给某人临时加个权限"的诉求会诱导实现成"allow 即可用"，
>    那等于**越权即升权** —— 与 `policy.py` 就 `clearance` 与 `role` 警告的是同一类错误。
>    所以 `deny` 必须能覆盖角色默认，且**判定期在最后**。

#### 4.3.1 两条容易漏掉的约束（都有监管依据）

**（1）角色互斥（SoD）**

指引第十二条（回避制度）+ 办法第三十二条（岗位相互制衡）要求支持**互斥约束**：

```python
# 互斥对：同时持有即拒绝（不是"取其一"，是配不上就不许生效）
MUTUALLY_EXCLUSIVE: tuple[tuple[str, str], ...] = (
    # 改别人的权限 vs 审自己的账 → NIST AC-5 "管理访问控制者不得同时管理审计"
    ("platform.user.manage", "platform.audit.read"),
    # 跑研究结论 vs 用该结论下单（本项目无下单，但研究/交易的墙要留）
    ("research.analyze.run", "read:portfolio"),
)
```

> **实现要点**：互斥校验放在**赋权时**（拒绝创建冲突授权）+ **判定时**（双保险）。
> 只在判定时校验会让"错误授权"一直躺在表里，某次改动忘了校验就生效了。

**（2）高权操作 = break-glass 临时提权，不是常驻授权**

| 约束 | 落点 | 理由 |
|---|---|---|
| **禁止自批** | DB 层 `CHECK(approver_id <> requester_id)` + 触发器兜底 | 只在应用层校验会被"绕过接口直连库"或"将来新加的接口"破功 |
| **强制到期** | `expires_at NOT NULL` | 常驻高权账号是审计事故常见来源 |
| **申请/批准分离** | 两个 API + 两张表 | 一个接口做两件事 = 必然有人把两个字段都填自己 |

### 4.4 第五个正交维度（证券名单）

上面四层回答的都是"**这个人**能做什么"。但指引第十九~二十二条要求的是另一个轴：
"**这只证券**现在能不能碰"——与"这个人是谁"完全正交。

| 概念 | 语义 | 本项目的动作 |
|---|---|---|
| **观察名单** | 已/可能掌握敏感信息的公司或证券；**高度保密**，业务照常但受监控 | **只留痕、不拦截**（拦截会让名单本身暴露） |
| **限制名单** | 隔离与披露不足时，对相关业务活动**实施限制** | **拒绝写**（加自选/建板块/写档案），并明示"该标的在限制名单（原因/到期日）" |

```sql
CREATE TABLE dim_restricted_symbol (
    trade_date TEXT NOT NULL,        -- 按日生效（可回放，符合"钉死 trade_date"纪律）
    code       TEXT NOT NULL,
    list_kind  TEXT NOT NULL,        -- watch（观察）| restricted（限制）
    reason     TEXT NOT NULL,        -- 必填：合规要看"为什么"
    source     TEXT NOT NULL DEFAULT 'compliance',
    expire_at  TEXT,                 -- 名单不是永久的
    created_by TEXT NOT NULL,
    PRIMARY KEY (trade_date, code, list_kind)
);
```

> ⚠️ **两条诚实边界**：
> 1. **名单本身是敏感信息**（指引要求"高度保密"）→ 读接口必须能力码保护
>    （`compliance.symbol_list.read`，默认仅 compliance/auditor），
>    且**不能**在前端给普通用户显示"你搜的这只票在观察名单"。
> 2. 本项目**不含下单链路**（执行层在 `qmt/`）→ 限制名单只作用于**研究与自选侧**。
>    **不要声称本项目实现了交易合规拦截**。

### 4.5 与"模块可见性"的关系（前端也要权限）

```
GET /api/v1/me                      → { user, tenant, roles, clearance, wall }
GET /api/v1/me/capabilities         → ["quant.intraday.watchlist.write", ...]
GET /api/v1/me/modules              → { intraday: {visible, caps:[...]}, ... }
```

前端用 `modules` 渲染页签（`App.tsx:161` 的 8 个 + `QuantTabContainer` 的 3 个子模块），
点进去后端仍逐个能力码校验（**前端隐藏不是安全边界**）。

### 4.6 套餐层级（VIP / 试用 / 管理）与升级路径

**需求口径**：租户 = 套餐层级；管理员自选 ≤ 100 只、VIP ≤ 20 只、试用 ≤ 10 只；
按层级给不同权限。

#### ① 层级与角色的**正交**关系（这一节最容易被做错）

```
套餐层级（tenant.kind / tier）  →  决定"能用多少"：配额、上限、刷新频率、算力、导出额度
角色（role）                    →  决定"能碰什么"：能力码
```

| 场景 | 正确处理 | 错误做法（会造成越权或体验断裂） |
|---|---|---|
| 试用用户要有完整做T能力 | 试用租户 + `researcher` 角色（能力码给足） | ❌ 因为"试用"就砍掉能力码 → 试用用户看不到产品价值 |
| VIP 用户不能改别人权限 | 升级租户即可，**角色不变** | ❌ 写 `if tier == "vip": allow_admin` → **付费即超管** |
| 管理员要能管人 | `admin` 角色（能力码 `platform.user.manage`） | ❌ 靠 `is_admin` 布尔字段绕过能力码 |

> **一句话**：**层级管"量"，角色管"权"。** 需求里"给不同权限"应落成
> **"不同层级默认不同的能力码集合"**（层级 → 默认角色的映射），
> 而不是把层级写进每个 `if` 里。

#### ② 套餐配置落库（而不是写死在代码里）

```sql
-- 套餐（层级）定义
CREATE TABLE dim_tier (
    tier_code        TEXT PRIMARY KEY,        -- admin | vip | trial
    name             TEXT NOT NULL,
    watchlist_limit  INTEGER NOT NULL,        -- 自选**总只数**上限（去重算）：100/100/25
    pool_limit       INTEGER NOT NULL DEFAULT 1,   -- 可建几个自选池：VIP 5 / 试用 5
    pool_size_limit  INTEGER NOT NULL DEFAULT 10,  -- 单池最多几只
    sector_limit     INTEGER NOT NULL DEFAULT 0,   -- 自定义板块股池个数（§7.2.6⑤）
    sector_size_limit INTEGER NOT NULL DEFAULT 0,  -- 单板块成员上限
    profile_limit    INTEGER NOT NULL,        -- 可存"自定义参数"的标的数上限
    quote_interval_s INTEGER NOT NULL,        -- 报价刷新间隔（**全层级统一 5s**，见 §7.2.6④）
    score_interval_s INTEGER NOT NULL,        -- 打分重算间隔（**按层级分档**，因与个股参数绑定）
    max_ws_conns     INTEGER NOT NULL DEFAULT 2,
    daily_llm_tokens INTEGER NOT NULL DEFAULT 0,
    daily_backtests  INTEGER NOT NULL DEFAULT 0,
    default_role     TEXT NOT NULL,           -- 该层级新用户的默认角色
    is_active        INTEGER NOT NULL DEFAULT 1
);
INSERT INTO dim_tier VALUES
 -- tier, name, 自选总数, 池数, 单池, 板块数, 板块成员, 参数上限, 报价s, 打分s, WS, tokens, 回测, role, active
 ('admin','管理员',   100, 5, 100,  20, 200, 100,  5,  60, 4, 200000, 50,'admin',     1),
 ('vip',  'VIP客户',  100, 5, 100,  20, 200, 100,  5,  60, 2,  50000, 10,'researcher',1),
 ('trial','试用用户',  25, 5,   5,   3,  20,  25,  5, 180, 1,   5000,  2,'researcher',1);
```

> ⚠️ **报价列三档都是 5s —— 这是刻意的**（需求方 2026-09 澄清）：
> 报价是**批量公共数据**（一次 300~500 只、耗时几乎不随数量增长），
> 全宇宙 5s 全量刷新的成本只有 **≈2 请求/s / 1.6 Mbps / 1% 单核**（实测外推）。
> **没有理由让试用用户的涨跌幅比 VIP 慢** —— 那只会造成无谓的体验落差。
> 需要节流的是**逐只接口**（分时/5mK），而它按 F/H/L 分档（§7.2.6④），不是按付费层级。

> **读法**：`VIP = 5 个池 × 单池最多 100 只，但 5 个池加起来去重后 ≤ 100 只`；
> `试用 = 5 个池 × 单池最多 5 只 = 去重后 ≤ 25 只`。**两个上限都要校验**（§7.2.6①）。

**自选池需要一层新表**（"池"本身是实体，不再是 YAML 里的一个扁平列表）：

```sql
CREATE TABLE dim_user_pool (          -- 自选池（每用户最多 pool_limit 个）
    pool_id    TEXT PRIMARY KEY,
    tenant_id  TEXT NOT NULL,
    user_id    TEXT NOT NULL,
    name       TEXT NOT NULL,         -- "我的持仓" / "观察池" / "AI概念"
    sort_order INTEGER NOT NULL DEFAULT 0,
    pinned     INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE INDEX idx_pool_owner ON dim_user_pool(tenant_id, user_id, sort_order);

-- 自选条目挂到池上（原 dim_user_watchlist 加 pool_id；同一只票可属多个池）
--   PRIMARY KEY (tenant_id, user_id, pool_id, code)
--   总只数校验 = count(DISTINCT code) WHERE user_id=? （**按去重算**，见 §7.2.6①）
```

**上限校验必须在服务端**（前端限制只是体验）：

```python
# POST /intraday/pools 与 /intraday/pools/{id}/stocks 的守卫（伪码）
tier = await tier_of(principal)                    # 从 dim_tenant.kind 查，不信客户端

# ① 池数
if await pool_count(principal) >= tier.pool_limit:
    raise HTTPException(403, f"{tier.name}最多 {tier.pool_limit} 个自选池")

# ② 单池只数
if await pool_size(pool_id) >= tier.pool_size_limit:
    raise HTTPException(403, f"单池上限 {tier.pool_size_limit} 只")

# ③ 总只数（★ 按去重算：同一只票放多个池不重复计数）
total = await distinct_watch_count(principal)
if code not in await distinct_codes(principal) and total >= tier.watchlist_limit:
    raise HTTPException(403, f"{tier.name}自选总数上限 {tier.watchlist_limit} 只"
                             f"（当前 {total} 只，去重计）")
```

#### ③ 个性化参数的归属（**回应"参数与用户绑定"的需求**）

需求原文："建议参数与用户绑定，如果用户有自定义参数就保存，没自定义参数就用系统自带的默认值，
用户修改的参数不能影响其他用户的参数。"

这正是 §3.3 的 **copy-on-write（继承）** 语义，落成一句话：

```python
async def effective_profile(user_id, code, mode):
    row = await repo.get(user_id, code, mode)       # ① 用户自己的（可能不存在）
    if row is not None:
        return row                                   # ← 有自定义：用他的
    tpl = await tier_template(user_id, code, mode)   # ② 套餐/平台默认模板
    if tpl is not None:
        return tpl                                   # ← 没自定义：用系统默认
    return DEFAULT_WEIGHTS[mode]                     # ③ 兜底：内置默认
```

**三条必须遵守的规则**（缺一条就会"串号"）：

| 规则 | 为什么 |
|---|---|
| **读的时候不要写**：读取走"用户档案 → 套餐模板 → 内置默认"三级回退，**不落库** | 否则"100 人 × 20 只"会在首次访问时膨胀出 2000 行空档案，且"改套餐默认值"对已访问过的人不再生效 |
| **只在用户真正改动时物化**（copy-on-write） | 用户没碰过的票永远跟着系统默认走 —— 这是"系统默认升级能惠及所有人"的前提 |
| **写入必须带 `user_id`**：`PRIMARY KEY (tenant_id, user_id, code, mode)` | 只带 `code` 就是现在 `dim_intraday_profile` 的错误（一只票全系统一份） |

> **存储量级**：`profile_limit` = 各层自选上限（100/20/10）。
> **最坏情况**（S2 设计点）全部用户都调满：管理员 1 × 100 + VIP 100 × 100 + 试用 300 × 25 = **17,600 行**；
> 每行 JSON 约 0.5~2KB（weights+thresholds+levels+character）→ **总 < 100MB**。
> 结论：**"单独调权重的票数很多"在存储上完全不构成压力**，不用为它设计任何特殊结构。
> （本节数字随"多池自选"需求更新，详见 §7.2.6⑥）

> **训练数据存哪**（本项目实际口径）：`data/quant` 走的是**文件+清单**（`manifest.json` +
> 分区目录），不是把训练矩阵塞进 DB。个人参数（权重/阈值）是**小 JSON** → 进 DB；
> 因子面板/模型文件是**大二进制** → 留文件系统（L0 共享）。两条路不要混。


---

## 五、数据模型

### 5.1 新增表（平台/租户/权限域）

```sql
-- 租户
CREATE TABLE dim_tenant (
    tenant_id     TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    kind          TEXT NOT NULL DEFAULT 'department',  -- department/team/personal
    wall_group    TEXT NOT NULL DEFAULT 'platform',
    quota_json    TEXT NOT NULL DEFAULT '{}',          -- 配额：LLM token/回测次数/并发
    is_active     INTEGER NOT NULL DEFAULT 1,
    created_at    TEXT NOT NULL
);

-- 用户
CREATE TABLE dim_user (
    user_id       TEXT PRIMARY KEY,
    username      TEXT NOT NULL,
    display_name  TEXT NOT NULL DEFAULT '',
    email         TEXT,
    password_hash TEXT NOT NULL,           -- bcrypt/argon2；禁止存明文
    is_active     INTEGER NOT NULL DEFAULT 1,
    is_locked     INTEGER NOT NULL DEFAULT 0,
    mfa_secret    TEXT,
    last_login_at TEXT,
    last_login_ip TEXT,
    login_count   INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL
);
CREATE UNIQUE INDEX uq_user_username ON dim_user(username);
CREATE UNIQUE INDEX uq_user_email    ON dim_user(email) WHERE email IS NOT NULL;

-- 成员关系（user ↔ tenant，含角色与清关）
CREATE TABLE dim_tenant_member (
    tenant_id  TEXT NOT NULL,
    user_id    TEXT NOT NULL,
    roles_json TEXT NOT NULL DEFAULT '[]',
    clearance  INTEGER NOT NULL DEFAULT 1,   -- DataClass: 0 PUBLIC 1 INTERNAL 2 CONF 3 RESTRICTED
    is_owner   INTEGER NOT NULL DEFAULT 0,
    joined_at  TEXT NOT NULL,
    PRIMARY KEY (tenant_id, user_id)
);

-- 角色与能力
CREATE TABLE dim_role (
    role_code TEXT PRIMARY KEY,               -- researcher/pm/trader/risk/compliance/auditor/admin
    name      TEXT NOT NULL,
    tenant_id TEXT,                           -- NULL = 平台内置
    priority  INTEGER NOT NULL DEFAULT 0,
    is_system INTEGER NOT NULL DEFAULT 1
);
CREATE TABLE dim_capability (
    code        TEXT PRIMARY KEY,             -- 'quant.intraday.watchlist.write'
    module      TEXT NOT NULL,                -- 'quant.intraday'
    description TEXT NOT NULL DEFAULT ''
);
CREATE TABLE map_role_capability (
    role_code TEXT NOT NULL,
    cap_code  TEXT NOT NULL,
    PRIMARY KEY (role_code, cap_code)
);

-- 授权：申请与批准**分两张表**（见 §4.3.1）
CREATE TABLE fact_grant_request (
    request_id     TEXT PRIMARY KEY,
    tenant_id      TEXT NOT NULL,
    requester_id   TEXT NOT NULL,
    target_user_id TEXT NOT NULL,
    cap_code       TEXT NOT NULL,
    effect         TEXT NOT NULL,             -- 'allow' | 'deny'
    reason         TEXT NOT NULL,
    ttl_hours      INTEGER NOT NULL,          -- break-glass 时长
    status         TEXT NOT NULL,             -- pending|approved|rejected|expired
    created_at     TEXT NOT NULL
);

CREATE TABLE fact_grant_approval (
    request_id   TEXT PRIMARY KEY REFERENCES fact_grant_request(request_id),
    requester_id TEXT NOT NULL,               -- 冗余一份，让触发器无需 JOIN
    approver_id  TEXT NOT NULL,
    decision     TEXT NOT NULL,               -- approved | rejected
    comment      TEXT NOT NULL DEFAULT '',
    decided_at   TEXT NOT NULL,
    -- 禁止自批：行内约束即可（**不能写子查询** —— SQLite 与 PostgreSQL
    -- 的 CHECK 都不允许引用其它行/表，这是常见误用）
    CHECK (approver_id <> requester_id)
);

-- 兜底：绕过应用层直插也不允许自批
CREATE TRIGGER trg_grant_no_self_approval
BEFORE INSERT ON fact_grant_approval
FOR EACH ROW
WHEN NEW.approver_id = (SELECT requester_id FROM fact_grant_request
                         WHERE request_id = NEW.request_id)
BEGIN
    SELECT RAISE(ABORT, '禁止自批：批准人不能是申请人');
END;

-- 生效中的授权（到期自动失效）
CREATE TABLE fact_permission_grant (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    tenant_id   TEXT NOT NULL,
    user_id     TEXT NOT NULL,
    cap_code    TEXT NOT NULL,
    effect      TEXT NOT NULL,                -- 'allow' | 'deny'（deny 优先）
    reason      TEXT NOT NULL,
    request_id  TEXT NOT NULL,
    approver_id TEXT NOT NULL,
    expires_at  TEXT NOT NULL,                -- 【强制】不许常驻高权
    revoked_at  TEXT,                         -- 提前回收（对应"回墙"）
    created_at  TEXT NOT NULL
);
CREATE INDEX idx_grant_effective
    ON fact_permission_grant(tenant_id, user_id, cap_code, expires_at);

-- 会话/令牌（补现有 HMAC 无过期的短板）
CREATE TABLE fact_session (
    session_id   TEXT PRIMARY KEY,
    user_id      TEXT NOT NULL,
    tenant_id    TEXT NOT NULL,
    console      TEXT NOT NULL DEFAULT 'default',  -- research | invest（§9.4）
    token_jti    TEXT NOT NULL UNIQUE,
    refresh_hash TEXT,
    ip           TEXT,
    user_agent   TEXT,
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    revoked_at   TEXT
);

-- 证券名单（第五维，§4.4）
CREATE TABLE dim_restricted_symbol (
    trade_date TEXT NOT NULL,
    code       TEXT NOT NULL,
    list_kind  TEXT NOT NULL,          -- watch | restricted
    reason     TEXT NOT NULL,
    source     TEXT NOT NULL DEFAULT 'compliance',
    expire_at  TEXT,
    created_by TEXT NOT NULL,
    PRIMARY KEY (trade_date, code, list_kind)
);
```

### 5.2 已有表 → 归属改造清单（实测 33 张表的完整分诊）

| 表 | 现有行数 | 归属 | 改造 |
|---|---:|---|---|
| `fact_data_points` | 2,816,211 | **L0** | **不动**（登记进 `GLOBAL_TABLES` 并写原因） |
| `sector_crowding_*` / `sector_member` / `sector_meta` / `dim_concept` / `map_stock_concept` | 217万~2517 | **L0** | 不动 |
| `auction_snapshot` / `auction_feature` / `auction_pick` / `auction_run` | 311/225/325/5 | **L0** | 不动（已在 `GLOBAL_TABLES` 登记） |
| `mainline_score` / `mainline_alert` / `mainline_*` | 94,219/2,865 | **L0**（回测结果 L1） | 回测报告加 `tenant_id`（部门级共享），其余不动 |
| `fact_events` | 794 | **L0** | 全市场事件，不动 |
| `fact_alerts` | 7 | **L2 + 共享** | 事件行 L0；**已读态/提醒设置**拆到 `fact_alert_state` |
| `dim_intraday_profile` | **2** | **L2** | **主键改 `(tenant_id, user_id, code, mode)`**（§6.1） |
| `dim_fund_flow_watch` | 36 | **L2** | 加 `(tenant_id, user_id)` + 改主键 |
| `sector_crowding_watch` | 7 | **L2** | 同上 |
| `sector_crowding_list` | 1,225 | **L1/L2** | 默认清单 L1，用户自建 L2（`user_id` NULL = 租户默认） |
| `sector_crowding_alert` | 0 | **L2** | 加 `(tenant_id, user_id)` |
| `auction_watch` / `auction_watch_score` | 4/5 | **L2** | 加 `(tenant_id, user_id)` |
| `dim_quant_sector` / `map_quant_sector_stock` | 0/0 | **L1/L2** | 加 `tenant_id`+`user_id`+`visibility` |
| `fact_quant_selection` / `_item` | 39/387 | **L0**（跑批结果）+ **L2**（谁加进自选） | 结果 L0；加自选写 L2 的 `dim_user_watchlist` |

### 5.3 用户私有新表

```sql
-- 自选股池（替代 configs/intraday.yaml 的 watchlist 段）
CREATE TABLE dim_user_watchlist (
    tenant_id      TEXT NOT NULL,
    user_id        TEXT NOT NULL,
    code           TEXT NOT NULL,
    name           TEXT NOT NULL DEFAULT '',
    boards_json    TEXT NOT NULL DEFAULT '[]',   -- 关联板块（用户声明的产业链关联）
    overseas_json  TEXT NOT NULL DEFAULT '[]',   -- 海外映射
    peers_json     TEXT NOT NULL DEFAULT '[]',   -- 同业池
    pinned         INTEGER NOT NULL DEFAULT 0,
    sort_order     INTEGER NOT NULL DEFAULT 0,
    note           TEXT NOT NULL DEFAULT '',
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL,
    PRIMARY KEY (tenant_id, user_id, code)
);

-- 个股口径档案（替代 dim_intraday_profile 的旧主键）
CREATE TABLE dim_intraday_profile_v2 (
    tenant_id      TEXT NOT NULL,
    user_id        TEXT NOT NULL,
    code           TEXT NOT NULL,
    mode           TEXT NOT NULL DEFAULT 'intraday',  -- intraday | daily
    weights_json   TEXT NOT NULL DEFAULT '{}',
    thresholds_json TEXT NOT NULL DEFAULT '{}',
    levels_json    TEXT NOT NULL DEFAULT '{}',
    character_json TEXT NOT NULL DEFAULT '{}',
    template       TEXT NOT NULL DEFAULT '',
    visibility     TEXT NOT NULL DEFAULT 'private',   -- private|tenant|tenant_edit|public
    source         TEXT NOT NULL DEFAULT 'manual',
    updated_at     TEXT NOT NULL,
    PRIMARY KEY (tenant_id, user_id, code, mode)
);

-- 提醒设置与已读态（个人）
CREATE TABLE dim_user_alert_pref (
    tenant_id TEXT NOT NULL,
    user_id   TEXT NOT NULL,
    channel   TEXT NOT NULL,                    -- feishu/dingtalk/wecom/email/ws
    target    TEXT NOT NULL DEFAULT '',         -- webhook 变量名（**不存明文地址**）
    min_level TEXT NOT NULL DEFAULT 'hint',     -- hint|solid|forced_exit
    enabled   INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (tenant_id, user_id, channel)
);
CREATE TABLE fact_alert_state (
    tenant_id TEXT NOT NULL,
    user_id   TEXT NOT NULL,
    alert_id  TEXT NOT NULL,
    read_at   TEXT,
    muted_until TEXT,
    PRIMARY KEY (tenant_id, user_id, alert_id)
);

-- 个人策略/条件
CREATE TABLE dim_user_strategy (
    strategy_id  TEXT PRIMARY KEY,
    tenant_id    TEXT NOT NULL,
    user_id      TEXT NOT NULL,
    name         TEXT NOT NULL,
    kind         TEXT NOT NULL,                 -- screen | condition | backtest_config
    payload_json TEXT NOT NULL,
    visibility   TEXT NOT NULL DEFAULT 'private',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);

-- 审计（可检索 + 保留哈希链字段）
CREATE TABLE fact_access_audit (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    at         TEXT NOT NULL,
    user_id    TEXT NOT NULL,
    tenant_id  TEXT NOT NULL,
    method     TEXT NOT NULL,
    path       TEXT NOT NULL,
    status     INTEGER NOT NULL,
    latency_ms INTEGER NOT NULL DEFAULT 0,
    action     TEXT NOT NULL DEFAULT '',        -- 能力码（业务动作级留痕）
    resource_kind TEXT NOT NULL DEFAULT '',
    resource_id   TEXT NOT NULL DEFAULT '',
    auth_source   TEXT NOT NULL DEFAULT '',
    chain_hash TEXT NOT NULL,
    extra_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX idx_audit_tenant_at ON fact_access_audit(tenant_id, at);
CREATE INDEX idx_audit_user_at   ON fact_access_audit(user_id, at);
```

> **保留 JSONL**：`data/audit/access_audit.jsonl` 继续写（不可篡改的第二副本）；
> 新表是**查询副本**，两者哈希链各自独立。
> 不要用新表替换 JSONL——"审计同库同权限等于没审计"。

### 5.4 行级隔离登记表要重写

`src/infrastructure/security/rls.py` 的 `TENANT_COLUMNS` 目前登记的是
`watchlist`/`intraday_profile`/`strategy`/`backtest_run`/`research_task`/
`screener_run`/`alert_rule` —— **与真实表名全部不符**。改为**两个登记表**：

```python
#: 租户级表 → 租户列
TENANT_COLUMNS = {
    "dim_user_watchlist": "tenant_id",
    "dim_intraday_profile_v2": "tenant_id",
    "dim_user_strategy": "tenant_id",
    "dim_fund_flow_watch": "tenant_id",
    "sector_crowding_watch": "tenant_id",
    "sector_crowding_alert": "tenant_id",
    "auction_watch": "tenant_id",
    "fact_alert_state": "tenant_id",
    "fact_permission_grant": "tenant_id",
    "fact_access_audit": "tenant_id",
    "mainline_backtest": "tenant_id",     # 回测报告部门级共享
}

#: 【新增】用户级表 → (租户列, 用户列)。仓储层必须**两个条件都下推**。
USER_COLUMNS = {
    "dim_user_watchlist": ("tenant_id", "user_id"),
    "dim_intraday_profile_v2": ("tenant_id", "user_id"),
    "dim_user_strategy": ("tenant_id", "user_id"),
    "dim_fund_flow_watch": ("tenant_id", "user_id"),
    "sector_crowding_watch": ("tenant_id", "user_id"),
    "sector_crowding_alert": ("tenant_id", "user_id"),
    "auction_watch": ("tenant_id", "user_id"),
    "dim_user_alert_pref": ("tenant_id", "user_id"),
    "fact_alert_state": ("tenant_id", "user_id"),
}
```

新增 `user_column_guard(table, alias="") -> (sql, params)`，语义与
`tenant_column_guard()` 一致（未登记即抛错、无身份即抛错）。
DB 层策略同步扩展：

```sql
-- 租户级：同租户可见
CREATE POLICY t_isolation ON dim_user_strategy
  USING (tenant_id = current_tenant_id()) WITH CHECK (tenant_id = current_tenant_id());

-- 用户级：仅本人（管理员**不在**策略内，读他人数据必须走审计员角色 + 显式跨墙）
CREATE POLICY u_isolation ON dim_user_watchlist
  USING (tenant_id = current_tenant_id() AND user_id = current_user_id())
  WITH CHECK (tenant_id = current_tenant_id() AND user_id = current_user_id());
```

配套**五条**必须做的工程细节（全部有官方文档或公开案例依据）：

1. **事务级 `SET LOCAL`，不是会话级 `SET`**：在显式事务内
   `SELECT set_config('app.tenant_id', $1, true)`（第三参 `is_local=true`），
   随事务提交自动失效；裸 `SET` 在连接池回收后**值仍在**，
   下一个请求会以**上一个人的身份**被过滤 —— RLS 上线后最常见的越权事故。
   落点：`src/infrastructure/repositories/postgres_repo.py` 的连接获取/归还路径，
   **封装在数据访问层，禁止业务代码自己拼**。
2. **`FORCE ROW LEVEL SECURITY` 不能省**：只 `ENABLE` 时**表 owner 默认绕过**；
   **superuser 与 `BYPASSRLS` 角色永远绕过** → 应用必须用
   **非 owner、非 superuser、非 BYPASSRLS** 的角色连库。
3. **小心"参照完整性检查绕过 RLS"的隐蔽信道**：外键检查**不受 RLS 约束**，
   可借它探测"他租户是否存在某行"；`TRUNCATE` 同样不受策略约束。
   → 用户级表**不要**用外键指向其它租户的数据。
4. **策略里避免子查询竞态**：READ COMMITTED 下策略子查询可能读到**旧权限快照**。
5. **system 身份也走同一套隔离**：后台任务**不要用 `BYPASSRLS` 角色**，
   而是显式 `SET LOCAL app.tenant_id` —— 与本项目 `system_scope(reason)` 的意图一致
   （但**当前生产代码零调用**，这是要补的接线）。

> **业务表策略之上还要一层**：`fact_grant_approval` 的"批准人 ≠ 申请人"
> 用 `CHECK` + 触发器双保险 —— `CHECK` 里**不能写子查询**
> （SQLite 与 PostgreSQL 都不允许跨行/跨表引用），这是很常见的误用。

---

## 六、逐模块改造清单

### 6.1 量化交易 · 做T辅助（改造量最大，先做这个）

| 项 | 现状 | 改法 |
|---|---|---|
| 自选池 | `configs/intraday.yaml` 的 `watchlist` 段（45 只，前端可写） | 落 `dim_user_watchlist`；YAML 降级为**平台默认模板**（新用户首登时 copy 一份） |
| 个股权重档案 | `dim_intraday_profile`，`code` 主键 | 迁 `dim_intraday_profile_v2`，主键 `(tenant,user,code,mode)`；现有 2 行归给 `admin` |
| `overrides` 段 | YAML，手工编辑 | 保留（平台级默认覆盖）；优先级：**用户档案 > 租户模板 > YAML overrides > 全局** |
| `pinned` | YAML `pinned: true` | 落 `dim_user_watchlist.pinned` |
| 30 个进程内缓存字段 | `_profile_cache`（全局）、`_fit_cache[(code,date)]`、`_watch_cache`、`_daily_cache`、`_character_cache`、`_chip_cache`… | ① `_profile_cache` → 按 `(tenant,user)` 分区；② `_fit_cache` → 键加**口径指纹** `hash(weights+thresholds+levels)`；③ `_watch_cache` → 键 `(tenant,user)`；④ `_character_cache`/`_chip_cache` 与用户无关 → **保持全局**（这是共享红利） |
| `watchlist()` 整表重算 | 一人加票 → `invalidate_watchlist_cache()` 全表作废 | 只失效**该用户的条目**；`_watch_generation` 按用户维护（否则 A 的改动会把 B 的 WS 推送刷成新版本） |
| `snapshot()` 无结果共享 | WS 每连接各算一次（`intraday.py:396`） | 加**单飞 + 按口径共享**（§7.1） |
| `_fit_cache` 拟合触发 | `level_fit` 0.5~5s，在请求路径上 | 移到后台队列（§7.3），面板先出规则口径 + "拟合中" |
| 提醒（`notifier.py`） | 全平台同一套 webhook 环境变量 | 落 `dim_user_alert_pref`；按**该用户**的渠道推送 |
| 提醒冷却 | `cooldown_minutes` 进程内 | 键加 `user_id`（A 收到过 ≠ B 收到过） |

> **`_push()` 的隐私红线**：当前只要信号触发就推送。多用户下
> **每个用户看同一只票都会触发一次** → 既刷屏又会把"某人的自选"暴露到共享 webhook。
> 必须改为**只在"该用户的池子里这只票触发"时，推给该用户的渠道**，冷却键含 `user_id`。

### 6.2 量化交易 · 量化选股 / 竞价选股

| 项 | 改法 |
|---|---|
| `quant_select_service.py`（50KB）跑批 | 能力码 `quant.selector.run`；**进独立 worker**（§8.2）；结果 `fact_quant_selection` 保持 L0（全平台一份结果） |
| 自定义板块 `dim_quant_sector` | 加 `tenant_id`+`user_id`+`visibility`（默认 private；可 `tenant` 分享给部门） |
| `POST /quant/select/add-to-watchlist(-batch)` | 写**当前用户**的 `dim_user_watchlist`（现在是写全局 YAML） |
| `auction_watch` | 加 `(tenant_id, user_id)`；竞价**结果**保持 L0 |
| 竞价调度（09:15/09:25） | 保持**全局单次**（全平台一份结果）→ 必须用分布式锁（§8.4） |
| `POST /quant/select/train` | 收紧到 `quant.selector.train`（默认仅 admin）+ 四眼；产物落 L0 |

### 6.3 投研分析（19 Agent）

| 项 | 改法 |
|---|---|
| `POST /research/analyze` | 能力码 `research.analyze.run`；**按租户配额**（token/次数）限流 |
| 任务表 | in-process `TaskStore` → 落 `fact_research_task(tenant_id, user_id, ...)`，支持重启恢复 + 跨副本查询 |
| 任务产物/报告 | L1 或 L2 由入参 `visibility` 决定，默认 L2 |
| 导出 | 已有 `requires_four_eyes` → 真接审批流（现在只是标注） |
| `/research/{task_id}` 读 | 必须校验 `tenant_id`+`user_id`（否则改个 id 就能读别人的分析） |

### 6.4 主线挖掘 / 资金流 / 板块拥挤度

| 模块 | 读（L0） | 个人写（L2） | 重操作 |
|---|---|---|---|
| 主线 | snapshot/saved/alerts/futures/backtest 列表 | 关注清单 → `dim_user_*` | `refresh`/`data/sync`/`backtest/run`/`futures/calibrate` → 能力码 + worker + 锁 |
| 资金流 | `snapshot`/`search`/`pick` | `dim_fund_flow_watch` 加用户维度 | — |
| 拥挤度 | `latest`/`metrics/*`/`sectors` | `watchlist`/`config_list`/`alert` 加用户维度（`config_list` 1225 行 → 租户默认 + 用户覆盖） | `refresh_all`/`recompute`/`metrics/compute` → worker + 锁 |

> **`POST /sector_crowding/config_list/reset` 这类"全局重置"端点**是多租户下最危险的：
> 一个用户可以把所有人的清单抹掉。→ 走"租户默认层 + 用户覆盖层"，
> `reset` 只重置**当前用户**的覆盖层。

### 6.5 调度管理 / 运行指标 / 事件告警

| 项 | 改法 |
|---|---|
| `GET /scheduler/jobs` | `scheduler.read`（默认 admin + risk/compliance/auditor） |
| `POST /scheduler/jobs/{name}/run` | `scheduler.job.run`（**默认仅 admin**）+ 四眼 |
| `GET /metrics/*` | `metrics.read`；`/healthz` 保持公开（探针需要） |
| 告警设置/已读 | `dim_user_alert_pref` + `fact_alert_state` |
| `POST /alerts/scan` | `alerts.scan.run`（花钱：两阶段 LLM）+ 租户配额 |
| `POST /events/import` | `platform.source.manage`（四眼） |

### 6.6 平台管理（新增模块）

> **完整接口清单见 §8.6.10.4**（含生命周期、续期、软删除、批量、层级配置）。
> 这里只列与模块边界有关的入口。
>
> **新增一个模块的边界判断**：管理员控制台是**新页签**（不是塞进某个现有页签）。
> 前端落点见本节末尾。

```
# —— 身份与自助 ——
POST   /api/v1/auth/register                 自助注册 → 落 pending（§8.6）
POST   /api/v1/auth/login | /refresh | /logout
GET    /api/v1/me, /me/capabilities, /me/modules

# —— 管理员控制台（§8.6.10，能力码 platform.user.manage，写操作四眼）——
GET    /api/v1/admin/overview                概览卡片
GET    /api/v1/admin/users                   用户列表（筛选/排序/分页）
POST   /api/v1/admin/users/{id}/approve      批准（指定 tier + **valid_until**）
POST   /api/v1/admin/users/{id}/reject       驳回（note 必填）
POST   /api/v1/admin/users/{id}/disable|enable|renew|tier|roles
DELETE /api/v1/admin/users/{id}?mode=soft|anonymize   **软删除/匿名化**（§8.6.1.3）
POST   /api/v1/admin/users/batch             批量（≤50）
GET    /api/v1/admin/reviews                 生命周期流水
GET    /api/v1/admin/tiers                   层级与配额配置

# —— 授权与合规 ——
POST   /api/v1/admin/grants/request          申请（break-glass）
POST   /api/v1/admin/grants/{id}/approve     批准（**禁止自批**）
GET    /api/v1/admin/tenants                 租户（= 套餐层级）管理
GET    /api/v1/admin/audit                   platform.audit.read（compliance/auditor）
GET    /api/v1/compliance/symbol-list        compliance.symbol_list.read（高度保密）
```

**前端落点**：本项目前端在 `web/src/App.tsx:161` 的 8 个顶层页签之外**新增一个
「平台管理」页签**（仅拥有 `platform.user.manage` 或 `platform.audit.read` 的角色可见）。
不要把它塞进「运行指标」——那个页签的定位是**可观测**，不是**运营动作**。

---

## 七、共享计算层（性能的核心，也是"能不能到 100 人"的答案）

### 7.1 四层缓存与单飞

```
请求 → ① 结果单飞：key = (code, mode, 口径指纹)
        ├ 命中 → 直接返回（100 人看同一只票 = 1 次计算 + 100 次序列化）
        └ 未命中 → 交给 ②
     ② 因子层（L0，与用户无关）：key = (code, trade_date)
        ├ bars/trend/quote/板块/缠论/chip/character/情绪周期 → 进程内 TTL + Redis 共享
        └ 命中率应 > 95%（同一交易日内同一只票的因子只算一次）
     ③ 个性化层：套用户权重 → 总分/档位/信号（毫秒级，无需缓存或按口径指纹短缓存）
     ④ 序列化层：payload 50KB/次 → 按 (code, 口径指纹) 缓存 model_dump 结果
```

**量级估算**（100 人 / 人均 15 只自选 / 每人同时看 1 只票 / 每请求 0.3s CPU）：

| 项 | 现状（无共享 + 事件循环内计算） | 目标（共享 + 计算移出事件循环） |
|---|---:|---:|
| 快照请求到达率 | 100 路 WS / 15s ≈ **6.7 次/s** | 同左 |
| 实际计算次数 | **6.7 次/s**（每连接各算一遍） | 按 `(code, 口径指纹)` 去重 ≈ **0.5 次/s** |
| 纯 CPU 需求 | 6.7 × 0.3 = **2.0 核** | 0.5 × 0.3 = **0.15 核** |
| 可否并行 | ❌ **不可**：同步 pandas 跑在事件循环里，一次只推进一个请求 | ✅ 可：`asyncio.to_thread` / 计算进程池 |
| **实际吞吐上限** | **实测 5~6 请求/秒**（即"1 请求 × 1 条事件循环"的上限） | 受核数约束：16 线程下 ~30+ 请求/秒（`设计推演`） |
| 1 条事件循环能否扛住 | ❌ 不能（6.7 > 6，必然排队 → 实测 p50 已到 6.5s） | ✅ 能（0.5 次/s 计算 + 毫秒级个性化） |

> **两个必须同时做的动作，缺一不可**：
> ① **共享**（6.7 → 0.5 次/s）—— 减少总工作量；
> ② **把计算移出事件循环**（`asyncio.to_thread` + 有界线程池）—— 让剩下的能真正并行。
> 只做 ① 也能过，但没有余量；只做 ② 会把 2.0 核的活摊到多核上，
> 看似不卡了，实则总工作量没降（数据源仍被打 6.7 次/s）。

> **不要用"加副本"解决这件事**：压测期间整机 CPU 只用了 **6~13%**，
> 说明瓶颈**不是核数**，是"1 条事件循环 × 0.3s 同步计算"。
> 这个前提下加副本只会把同一份数据源请求放大 N 倍（且 §8.1 的 QMT 锁不允许）。

> 关键洞察：**"100 人"里真正稀缺的是"不同的 (股票, 口径) 组合数"，不是人数。**

### 7.2 实时数据供给层：活跃宇宙 + 分档刷新（**回答"多人同时用会不会卡在取数上"**）

> 这一节是本次需求的核心。先看**实测**约束，再给方案。
> 所有数据来自 `scripts/_probe_datasource.py`（只读公网行情，可复跑）。

#### 7.2.1 实测：数据源的真实约束（`实测`）

| 接口 | 能力 | 实测 |
|---|---|---|
| **批量快照** `qt.gtimg.cn/q=` | **一次 ≤60 只** ✅ | 60 只 **26.7KB / 53ms**（有效行 56）；30 只 13.3KB/89ms |
| **5 分钟 K 线** `ifzq.gtimg.cn/.../mkline` | **逐只，不支持多只一次** ❌ | 320 根 **24KB / 93~162ms**（实测 3 只多代码请求只回 41B、0 bar） |
| **分时** `web.ifzq.gtimg.cn/.../minute/query` | **逐只** ❌ | 全天 **9.3KB / 66~92ms**（多代码请求回 46B） |
| 逐只 5mK 并发 | 并发线性扩展 | 40 只：conc4 **1.05s** / conc8 **0.53s** / conc16 **0.30s** → **800 只 ≈ 6.1s** |

**由此得到三条硬结论**：

1. **数据源压力只与"去重后的不同股票数"成正比，与"多少人"无关。**
   100 人各 20 只自选，去重后约 **300~500 只**（热门票高度重叠），不是 2000 只。
   这正是"共享"能救命的根本原因。
2. **报价可以很便宜**：800 只 ÷ 60 = **14 次请求**，约 **0.8s 墙钟 / 1.5% 单核**
   → 报价按"活跃宇宙"每 5 秒全量刷都是可行的。
3. **分钟 K 线是真瓶颈**：800 只逐只 ≈ **6.1s**（16 并发）。若每只每 5 分钟刷一次，
   平均 **2.7 只/s**（≈0.3s 墙钟/s，可接受）；**但如果每只每 15 秒刷一次，
   就是 53 只/s ≈ 6s 墙钟/s（已饱和）** —— 所以**绝不能让"用户在看"决定刷新频率**。

#### 7.2.2 方案：活跃宇宙（Active Universe）+ 四档 TTL

**核心思路：把"每个人各自下载自己要的"换成"全局按标的下一次、所有人都读缓存"。**

```
① 宇宙 = 所有用户自选的并集（去重）     ← 动态维护，增删自选即更新
   ├─ 热度分：被几个用户的自选包含 + 是否为"当前有人正在看"
   └─ 优先级：正在看(foreground) > 自选列表(background) > 长尾

② 分档刷新（频率按"数据变化速度 + 是否有人正在看"，不按用户数）
   ┌ 报价快车道   批量60只/请求   5s(前台) / 30s(后台) / 60s(长尾)   ← 见 §7.2.6   ├ 分时(1分钟)   逐只            60s(前台) / 300s(后台)
   ├ 5分钟K线      逐只            300s（数据本身 5 分钟才变一次 —— 刷更勤是纯浪费）
   └ 日线/估值/新闻 逐只/TTL        1800s / 3600s / 600s（沿用现有配置）

③ 读路径：**内存命中，永不冷取**
   用户打开页面 → 从预取的缓存直接出数（实测 /watchlist 命中缓存 0.05s）
   ├ 未命中（长尾票）→ 单飞取数一次并写入宇宙（去重后只此一次）
   └ 用户点开某只票 → 复用它已命中前台档位的数据，**不额外打数据源**
```

#### 7.2.3 关键设计判断（面试会追问"凭什么"）

| 判断 | 理由（都有实测支撑） |
|---|---|
| **绝不能为用户打开页面就下载 320 根 5mK** | 单只 24KB / ~100ms；100 人各看 3 只 = 7.2MB + 300 次请求，而且**绝大多数是重复的**（同一只热门票被反复取） |
| **报价与打分解耦**（现有设计，保留） | 报价 53ms/60 只，打分要 0.3s/只 → 两者差一个数量级，不该用同一个 TTL |
| **打分按"活跃宇宙"增量重算，不按用户整表** | 现有 `_watchlist_refresh_loop` 是**每用户**每分钟重算全部自选 → 100 人 × 20 只 = 2000 次/分钟；改为按 `(code, 口径指纹)` 去重后约 300~500 次/分钟 |
| **前台优先级 > 后台**：用户正看的票进"快档" | 人眼只盯 1~3 只。让 100 人的"正在看"去决定刷新集合，规模是 100~300 只，而不是 2000 只 |
| **长尾票用 stale-while-revalidate**（现有模式，保留） | 长尾票的变化对用户价值低；先给旧值再后台补，比让用户等好 |

#### 7.2.4 峰值窗口：本系统的负载是**尖峰型**，不是平稳型

**需求方给的实测事实**：用户使用时间短，**绝大多数集中在 9:20–10:30**（约 70 分钟）。
这条信息会**推翻"按日均摊"的所有估算**——必须按峰值设计。

**峰值时刻的并发结构**（100 人同时活跃，全部在 9:25 集合竞价到 9:35 开盘那段）：

| 事实 | 数值 | 为什么 |
|---|---|---|
| 同时在线的 WS 连接 | 100 | 每人 1 条（§4.6 `max_ws_conns` 限 1~4） |
| 同时"正在看"的标的 | **≈100 只** | **一个人同一时刻只打开一个做T面板**（前端只有一个 `active` 标的）→ 100 人就是 100 只，**不论他有几个池、多少只自选** |
| 自选列表重算 | ≈100 轮 / 周期 | 每用户一份（**只重算当前池**，不是 5 个池全算），可被 `(code, 口径指纹)` 去重；按 §7.2.2 走缓存 |
| **报价快车道**（F 前台 100 只） | 100 只 ÷ 60 = **2 请求 / 5s** | 批量 60 只/请求、53ms → **0.4 req/s，可忽略** |
| **分时（F 前台 100 只 @60s）** | 100 只 ÷ 60s = **1.7 只/s** | × 80ms → **0.13s 墙钟/s**（并发 8 时 ≈ 2% 单核） |
| **5mK（F 100@120s + H 800@300s）** | 0.83 + 2.67 = **3.5 只/s** | × 100ms → 0.35s 墙钟/s（并发 16 ≈ 2%） |
| **合计前台+热门档峰值** | **≈5 请求/s、< 0.4 核** | 远低于数据源实测能力（≈130 req/s） |

> **关键洞察（也是这套设计成立的根本）**：
> **峰值需求由"100 个用户各自正在看的那一只票"决定，而不是由"100 人 × 各自的全部自选"决定。**
> 按新需求，后者是 **17,600 条自选 / 2,000 只去重宇宙** ——
> **差 30 倍**（长尾档不主动刷，就是这个差距的来源）。
> 所以"F 前台 / H 热门后台 / L 长尾"三档不是优化，是**让峰值成立的必需设计**（§7.2.6④）。

**峰值的三个真实风险**（不是平均负载，而是这三条）：

| 风险 | 为什么会发生 | 应对 |
|---|---|---|
| **① 开盘瞬间的冷启动风暴** | 9:25 集合竞价→9:30 开盘，100 人几乎同一分钟进来，而宇宙有 **2,000 只**票**全部过期**（盘前数据非当日） | **盘前预热（§7.2.5a）**：09:15 起分批把宇宙拉成"当日已就绪"，用户进来就是热的 |
| **② WS 握手与首屏洪峰** | 100 条 WS + 100 次首屏并发建立 | 握手限流（admission control）+ 首屏走**占位数据 + 后台补**（现有 `_placeholder_watchlist` 模式，保留）+ 客户端退避重连 |
| **③ 09:30 分钟线首次生成** | 第一根 1 分钟 bar 在 09:31 才出现，所有人同时要它 | 分时档 TTL 对齐**分钟边界 +2 秒**（现有 `_seconds_until_next_tick` 已这么做，保留）；错峰取数（按 code hash 分散到 60 秒窗口内，避免整点齐发） |

**（a）盘前预热：把"洪峰"提前变成"缓坡"**

```
09:15 起（竞价开始前，**需求方已确认该窗口可接受**）：
  ├ ① 分批拉宇宙的 5mK：**2,000 只**，限速 **约 7 只/s** → 约 5 分钟，**09:20 前完成**
  │     （16 并发下每只 ~100ms → 0.7s 墙钟/s ≈ 0.7 核，盘前是空闲算力，不与人抢）
  ├ ② 分批拉当日分时：**只预热 F+H 档共 900 只**，限速 ~15 只/s → 约 1 分钟
  ├ ③ 预热日线/估值/股性画像（盘前即可确定，TTL 长，顺带做）
  └ ④ 错峰：按 code 的 hash 把 2,000 只摊到 5 分钟窗口，避免 09:15:00 瞬间齐发

09:25（集合竞价出结果）：
  ├ 集合竞价选股跑批（全平台一次，用分布式锁 §8.4）
  └ 把选股结果的票**插队进前台档位**（用户最可能马上点开这些）

09:30 开盘：
  └ 宇宙已就绪 → 用户请求全部命中内存，**零冷取**
```

> **为什么预热能省这么多**：预热把"100 个用户各自的冷取"合并成"一次按标的的批量取数"。
> 而且**预热发生在用户进来之前**，所以它占用的是**空闲算力**，不与人抢资源。
> 成本：2,000 只 × 24KB = **48MB 下行**（5mK）+ 900 只 × 9.3KB ≈ **8MB**（分时）
> ≈ **56MB / 约 6 分钟 / 16 并发下 < 1 核** —— 换来的是
> "开盘后 70 分钟峰值期内零冷启动"。这是本方案性价比最高的一处。
> ⚠️ 预热**必须分批限速**（5mK ≈7 只/s、分时 ≈15 只/s）：一次性放 2,000 个请求
> 会被数据源限流，那就把"预热"变成"自我 DDoS"。

**（b）峰值期的自适应降级（按优先级牺牲）**

峰值时若仍有压力，**牺牲顺序是预先定好的**（而不是随机变慢）：

| 优先级 | 数据 | 降级动作 | 用户可见性 |
|---|---|---|---|
| P0 不能降 | 前台标的的报价 / 分时 / 档位 / 信号 | 不降级 | —— |
| P1 降频率 | 后台自选列表的报价与打分 | 5s → 15s | 列表状态栏显示实际节奏（现有"⟳ 报价 5s"文案机制） |
| P2 降频率 | 后台标的的分时/5mK | 60s → 300s | —— |
| P3 延后 | 长尾票（无人看、低热度） | 15 分钟档，或**直接不刷**（用户点开时才取） | "该标的为长尾，首次加载中" |
| P4 关闭 | 非实时附加项：板块分时、海外映射、消息面 LLM | 暂停/降 TTL | 面板 gap 里明说"因负载已降级该维度" |

> **诚实原则**（与项目既有风格一致）：**降级必须让用户看得见**。
> 默默把 5 秒的报价变成 15 秒，会让用户以为"行情就是不动"，那是会误导交易的。

#### 7.2.5 治理：必须加的护栏（否则会把自己或数据源打死）

| 护栏 | 值（建议） | 为什么必须有 |
|---|---|---|
| **全局并发闸门** | 单数据源 ≤ 8~16 并发（实测 16 已到 0.30s/40 只） | 无上限的并发会被数据源限流/封 IP，而且是**服务级**故障 |
| **全局限速** | 按域名令牌桶（如 100 req/s 上限，留 3× 余量） | 实测能力 ~130 req/s，留余量应对突发 |
| **单请求去重（单飞）** | `lock:snap:<code>:<口径指纹>` | 已在 §7.1 定义；防同一只票被 6 路同时取（现有 per-key lock 只覆盖进程内） |
| **失败冷却** | 现有 `source_cooldown_seconds=300`，**多用户下要按域名共享** | 否则 100 个用户各触发一次冷却重试 = 100 倍无效请求 |
| **长尾降级** | 宇宙超过阈值（默认 **800 只**，见 §7.2.6）→ 超出部分降到 15 分钟档 | 给"少数人看冷门票"一个明确的降级策略，而不是拖垮全体 |
| **可观测** | `universe_size` / `fetch_rate{interface}` / `fetch_queue_depth` / `cache_hit_ratio` | 没有这四个数，无法判断"是数据源慢了还是我们打多了" |

#### 7.2.6 多池自选下的重算（**需求变更：每人 5 个池**）

> **需求更新**：自选股池不是"每人一只池"，而是
> **VIP：5 个自选池 × 共 100 只**；**试用：5 个自选池 × 5 只/池 = 共 25 只**。
> 另有**用户自定义板块股池**。本节据此**重算**，并指出一处**逼近数据源上限**的地方。

#### ① 配额模型（三层：池数 / 每池只数 / 总只数）

```sql
-- 套餐里加"池数"与"单池上限"（原 §4.6 只算了总数）
ALTER TABLE dim_tier ADD COLUMN pool_limit      INTEGER NOT NULL DEFAULT 1;  -- 可建几个自选池
ALTER TABLE dim_tier ADD COLUMN pool_size_limit INTEGER NOT NULL DEFAULT 10; -- 单池最多几只

UPDATE dim_tier SET watchlist_limit=100, pool_limit=5, pool_size_limit=100 WHERE tier_code='admin';
UPDATE dim_tier SET watchlist_limit=100, pool_limit=5, pool_size_limit=100 WHERE tier_code='vip';
UPDATE dim_tier SET watchlist_limit=25,  pool_limit=5, pool_size_limit=5   WHERE tier_code='trial';
```

**服务端三重校验**（缺任一条都能被绕过）：

| 校验 | 规则 | 不做的后果 |
|---|---|---|
| 池数 | `count(pools of user) <= pool_limit` | 建 1000 个池 |
| 单池 | `count(stocks in pool) <= pool_size_limit` | 一个池塞 2000 只绕过总数限制 |
| **总数** | `sum(stocks across pools, 去重) <= watchlist_limit` | 5 个池各 100 只 = 500 只（VIP 超 5 倍） |

> ⚠️ **"总数按去重算还是不去重算"必须写死**：建议**按去重算**
> （同一只票出现在两个池里只算一次）——否则用户会把同一只票放进 5 个池来变相扩容，
> 而数据侧确实只需要取一次。**规则要写在 API 文档里**，不然用户会觉得被多扣了。

#### ② 自选条目数与**去重后宇宙**（数据侧的真正分母）

> 🔄 **人数口径更新（需求方 2026-09）**：**当前 VIP 30 人，很快扩容，试用用户至少百人**。
> 因此本节按**两个档位**估算，并把**扩容目标**当设计点（不做"够用就好"的乐观假设）：
>
> | 情景 | VIP | 试用 | 用途 |
> |---|---:|---:|---|
> | **S1 当前** | 30 | 100 | 上线初期 |
> | **★ S2 扩容目标（设计点）** | **100** | **300** | 容量设计与压测基准 |
> | S3 远期（预留） | 300 | 300 | 只用于确认"加机器"的触发信号 |

**自选条目数（S2 设计点）**：

| 层级 | 人数 | 每人总只数 | 自选条目总数 | 占比 |
|---|---:|---:|---:|---:|
| VIP | 100 | 100 | **10,000** | 62% |
| 试用 | 300 | 25 | **7,500** | 47% |
| 管理员 | 1 | 100 | 100 | 可忽略 |
| **合计（未去重）** | | | **≈17,600 条** | |

**去重后宇宙**（需求方补充的关键事实：**"大部分用户自选股都是热门股"**）：

```
去重率由"标的有多热门"决定，所以需求方这句话直接把宇宙压小了：

S2（100 VIP + 300 试用，17,600 条自选）：
  ├ 若按均匀分布（每票被 ~9 人包含）：17,600 ÷ 9 ≈ 1,950 只
  └ ★ 按"大部分是热门股"（热门票被几十人包含）：实际去重更狠
      热门 200 只覆盖了 60% 的条目 → 剩余 40%(7,000 条)÷ 重复度 4 ≈ 1,750
      ≈ 200 + 1,750 = **约 1,900 只**  ← 与均匀估算惊人地接近
```

> **结论：S2 设计点 = 1,900 只（取整 2,000 只）**。
> 注意它**没有随人数上涨**：试用用户多但每人只 25 只、且都是热门股，
> 去重后增量很小。**这验证了"热门股共享"的威力：人数翻了 3 倍，宇宙几乎没变。**
>
> ⚠️ 但**第 2 条需求（自定义板块进宇宙）会把这个数字拉大**，见 §7.2.6⑧。

**🔒 参数锁定（2026-09 需求方确认）**

| 参数 | 锁定值 | 影响 |
|---|---|---|
| **人数（设计点 S2）** | **VIP 100 / 试用 300**（当前 30 VIP，扩容中） | 未去重自选条目 ≈17,600 |
| **去重宇宙（仅自选池）** | **≈1,900 → 取整 2,000 只** | 全部取数负载、内存、预热规模 |
| **自定义板块是否进宇宙** | **✅ 进**（但降一个数量级的刷新频率，§7.2.6⑧） | 宇宙会从 2,000 涨到 **≈3,500 只** |
| 自选池 | 每人 5 池；VIP 100 只 / 试用 25 只 | —— |
| **实时刷新的池数** | **每用户最多 1 个池**（当前查看的那个，§7.2.6⑨） | 这是**把宇宙变回可控的关键约束** |
| **F/H/L 分层** | **F=100 / H=800 / L≈2,600** | H 档容量 800 是**可调参数** |
| **盘前预热窗口** | **09:15–09:30 可接受**（已确认） | 可放心做预取 |
| 压测上限 | 按 **4,000 只**宇宙压（留 >10% 余量） | 压测要在最坏情景下不崩 |

#### ③ 重算：数据源负载（`实测` 外推，**宇宙 = 自选 2,000 + 自定义板块 ≈1,500 = 3,500 只**）

| 数据 | 单只成本（实测） | 一轮请求数 | 刷新周期 | 稳态速率 | 折算单核 |
|---|---|---|---|---|---|
| **报价**（批量，**不分档**） | 一次 **300 只 / 98ms** | 3,500 ÷ 300 = **12 请求** | **5s** | **2.4 req/s** | 2.4×0.098 = 0.24s/s ≈ **1.5%** |
| **分时**（逐只） | 9.3KB / 80ms | F 100 + H 800 | 60s / 300s | **4.3 只/s** | 单并发 0.34s/s；**16 并发 ≈ 2%** |
| 5mK（逐只） | 24KB / 100ms | F 100 + H 800 + L 2,600 | 120s / 300s / 900s | **6.4 只/s** | 单并发 0.64s/s；**16 并发 ≈ 4%** |
| 宇宙缓存内存 | 33KB/只（5mK+分时+报价） | 3,500 只 | 常驻 | —— | **≈115MB** |
| **合计** | | | | **≈13 请求/s** | **< 0.6 核 / < 6 Mbps** |

> 📌 **自定义板块的 1,500 只是"降一个数量级刷新"后的贡献**（§7.2.6⑧）：
> 它们的报价仍按 5s 刷（报价是批量、几乎免费），但**分时/5mK 走 900s / 1800s 的懒刷新**，
> 所以对逐只接口的压力只有上面表里的 L 档那部分。**若板块也按 60s 刷分时，
> 立刻多出 25 只/s → 2.0s 墙钟/s，直接吃掉 1/8 常驻核** —— 这就是必须降频的原因。

**三处必须点明的判断**：

| 灯 | 事实 | 处置 |
|---|---|---|
| 🔴 **"全宇宙分时 @60s"不可行** | `3,500 ÷ 60s = 58 只/s` → 单并发 **4.7s 墙钟/s（饱和）** | 分时档**只给"当前查看的那个池"（60s）+ H 热门（300s）**；其余不主动刷 |
| 🟡 **5mK @300s 到 0.64s/s（16 并发 ≈4%）** | 有余量；宇宙涨到 5,000 只时升至 0.9s/s | 5mK 保持 300s；**若宇宙 >5,000 只降到 600s** |
| 🟢 **报价不分档 —— 全宇宙统一 5s** | 批量快照**一次能带 300~500 只**，耗时几乎不随数量增长（60 只 62ms → 300 只 98ms） | **取消报价的层级分档**，全宇宙统一 5s（见 ④b） |

#### ③b 人数扩容对负载的影响（**关键：涨得比人数慢**）

需求方说"VIP 30 人很快扩容、试用至少百人"。**好消息是负载不随人数线性涨**：

| 情景 | VIP/试用 | 未去重自选 | 去重宇宙（仅自选） | 报价负载 | 结论 |
|---|---:|---:|---:|---:|---|
| S1 当前 | 30 / 100 | 5,500 | **≈1,300 只** | 0.9 req/s | 余量 140× |
| **★ S2 扩容目标** | **100 / 300** | **17,600** | **≈1,900 只** | 1.4 req/s | 余量 90× |
| S3 远期 | 300 / 300 | 37,600 | ≈2,000 只 | 1.4 req/s | 余量 90× |

> **为什么人数涨 3 倍、宇宙几乎不变**：需求方已经点明——**"大部分用户自选股都是热门股"**。
> 300 个用户各 25 只，去重后大部分落在同一批热门票上。
> **这是"共享"能吃到的最大红利：用户数增长 ≫ 资源消耗增长。**
>
> ⚠️ **但这条红利的前提是"热门股集中"**。如果哪天用户开始各看各的冷门票
> （例如给每个客户配了定制股票池），宇宙会向 S3 的 2,000 只以上漂移，
> 甚至逼近全市场 —— **所以要监控 `universe_size` 这个指标**（§9.1），
> 它是"红利还在不在"的直接读数。

#### ④ 分档重定义：**报价全量高频，只有逐只接口才分档**

> **需求方澄清（2026-09）**："在自选里的票，左侧自选股池的涨跌幅要**实时刷**；
> 这部分是**公共数据，可以实盘数据共享**，只不过**分发给多个用户**。"

这条澄清**推翻了我上一版的一个设计**——我原来把报价也按 F/H/L 分档（30s/60s），
那是把"逐只接口"的稀缺性错误地套到了"批量接口"上。**两者成本差一个数量级**：

| 接口 | 批量能力（`实测`） | 稀缺性 |
|---|---|---|
| **批量快照** `qt.gtimg.cn/q=` | **一次 300~500 只**；60 只 62ms、300 只 98ms、500 只 96ms —— **耗时几乎不随数量增长** | **不稀缺** |
| 分时 / 5mK | **只能逐只**（多代码请求回 41B / 0 bar） | **稀缺**（决定分档） |

**修正后的分档（重要）**：

| 档 | 范围 | 规模 | **报价** | 分时 | 5mK |
|---|---|---:|---|---|---|
| **F 前台** | 各用户**当前正在看**的那只票 | ≈100 只 | **5s** | **60s** | 120s |
| **H 热门后台** | 宇宙内**按热度排序的前 N 只**（N 默认 **800**） | 800 只 | **5s**（← 改） | **300s** | 300s |
| **L 长尾** | 宇宙内其余（≈1,100 只） | 1,100 只 | **5s**（← 改） | **不主动刷**（点开时才取） | 900s |

> **为什么报价可以全量 5s**（算给你看）：
> ```
> 2,000 只 ÷ 300 只/请求 = 7 请求 / 轮
> 每轮 5s  →  1.4 req/s；每次 ~98ms → 0.14s 墙钟/s（16 并发 ≈ 0.9% 单核）
> 下行：7 × 99KB = 693KB / 5s ≈ 1.1 Mbps
> ```
> **结论：整池 5 秒全量刷新的成本约 1.4 请求/s、1.1 Mbps、0.9% 单核 —— 可以忽略。**
> 所以**没有任何理由**让试用用户的涨跌幅比 VIP 慢 —— 那只会造成无谓的体验落差。

**重算后的稳态（宇宙 2,000 = F 100 / H 800 / L 1,100）**：

| 数据 | 计算 | 结果 |
|---|---|---|
| **报价** | 2,000 只 ÷ 300 只/请求 = **7 请求 / 5s** | **1.4 req/s** → 0.14s/s（16 并发 ≈ **0.9%**）✅ |
| 分时 | F 100÷60s + H 800÷300s + L 0 | **4.3 只/s** → 0.34s 墙钟/s（16 并发 ≈ **2%**）✅ |
| 5mK | F 100÷120s + H 800÷300s + L 1,100÷900s | 0.83 + 2.67 + 1.22 = **4.7 只/s** → 0.47s/s（16 并发 ≈ **3%**）✅ |
| **合计** | 峰值时 ×1.5 余量 | **≈10 请求/s、< 0.5 核、< 4 Mbps** ✅（能力 ~130 req/s，余量 ≈13×）|

#### ④b "取数共享"与"分发按用户"是两件事（需求方原话的关键区分）

需求方说的是"**公共数据可以共享，只不过分发给多个用户**"——这句话精确地描述了本系统的做法，
但**"共享"和"分发"必须分开讲**，否则会写出两种典型错误：

```
✅ 正确：
   取数（共享）：2,000 只票，全平台**只取 7 次请求 / 5s**，取回存在服务端缓存（L0）
   分发（按用户）：从缓存里**按每个用户的池子切片**，推给他自己的连接
                   → 100 个用户各收自己那 ≤100 只的报价，**不去重、不合并**

❌ 错误 A：把"共享"理解成"只推一份"
          → 用户看到的不是自己的池子（数据串号）

❌ 错误 B：把"分发"理解成"各取一次"
          → 100 个用户各打一次数据源（2,000 只 → 100 倍请求，端点必被限流）
```

**落点**（本项目已有雏形，本次要显式化）：

| 环节 | 实现 | 现状 |
|---|---|---|
| 取数共享 | `IntradayDataProvider.fetch_quotes(codes)` + `TencentSource._raw_quotes` 的 **5 秒共享缓存** | ✅ **已实现**（`sources.py:880` 注释里已写明"同一批或子集在窗口内复用"） |
| 宇宙级批量 | 新增：把全宇宙按 300 只分块，**全局刷一轮**写入共享缓存 | ⬜ 待实现（现在只按"某个用户的池子"取） |
| 按用户切片 | WS 推自选列表时从缓存**取该用户池子的子集** | ✅ 已实现（`_apply_quote_overlay` + `watchlist()`），但**当前卡片粒度是"当前标的"** |
| 报价帧轻量化 | 报价用**独立小帧**推送（`{type:"quotes", data:{code:[price,chg]}}`，约 1.2KB/池） | ⬜ **待改**：现在整份 watchlist（含分数/信号）一起推，帧大且被打分节奏拖累 |

> 🔑 **关键区分（这一条决定了用户看到的东西对不对）**：
>
> | 字段 | 与用户有关吗 | 能否全宇宙统一刷新 |
> |---|---|---|
> | **涨跌幅 / 现价** | ❌ 纯公共 | ✅ **5s 统一刷**（需求方要的就是这个） |
> | 总分 / 信号 / 档位 | ✅ 取决于**该用户的个股参数** | ❌ 按 `(code, 口径指纹)` 去重，不能统一算 |
>
> 所以左侧自选池要有**两条独立节奏**（现有 `service.py` 的"报价快车道 + 打分循环"
> 就是这个设计，**保留并显式化**）：
>
> ```
> 报价快车道（全量、共享、5s）  → 刷「现价 / 涨跌幅 / 成交额」 ← 需求方要的"实时"
> 打分循环（按用户、60s/180s）  → 刷「总分 / 信号 / 档位」    ← 与用户参数绑定，必须分开
> ```
>
> **状态栏必须把两条节奏都写出来**（现有文案"⟳ 报价 5s · 打分 60s"已做到，保留）——
> 否则用户会把 60 秒前的**信号**当成此刻的信号，那是**会误导交易**的。

> **关键设计判断（这一条是本节的核心）**：
> **"在自选里"不等于"需要实时刷新"。**
> 一个用户有 100 只自选，但他**同一时刻只看 1 只**；
> 其余 99 只的价值是"列表里有个分数"，而那个分数 **60~300 秒更新一次完全够用**。
> 所以分档必须按**关注度（是否有人正在看 / 被多少人自选）**，而不是按**归属（在不在自选里）**。

**"热度分"的定义**（决定谁进 H 档）：

```python
heat(stock) = w1 * (被多少个用户的自选包含)          # 主要项：公共关注度
            + w2 * (近 5 分钟被点开次数)              # 短期热度：有人正在看就是热点
            + w3 * (is_foreground)                    # 直接前台，最高优先
# 建议 w1=1.0, w2=0.5, w3=∞（前台直接置顶）
# 热度分每 30s 重算一次（低成本，只依赖计数），H 档 = top 800
```

#### ⑤ 用户自定义板块股池：容量与隔离

需求里"用户自定义板块的股池"在本项目**已有对应实现**（`dim_quant_sector` +
`map_quant_sector_stock`），本次只需**加用户维度与配额**：

```sql
ALTER TABLE dim_quant_sector ADD COLUMN tenant_id  TEXT NOT NULL DEFAULT '';
ALTER TABLE dim_quant_sector ADD COLUMN user_id    TEXT NOT NULL DEFAULT '';
ALTER TABLE dim_quant_sector ADD COLUMN visibility TEXT NOT NULL DEFAULT 'private';
-- 板块成员同样按 (tenant_id, user_id, sector_id) 隔离
```

**配额与容量**（必须设上限，否则"自定义板块"会成为绕过自选上限的后门）：

| 项 | 建议上限 | 理由 |
|---|---|---|
| 板块数/人 | `sector_limit`：VIP 20 / 试用 3 | 若不限量，用户可以建 100 个板块放 10,000 只票 |
| 板块成员数 | `sector_size_limit`：VIP 200 / 试用 20 | 同上 |
| **是否计入数据宇宙？** | ✅ **计入，但刷新频率降一个数量级** | 需求方 2026-09 明确要求（见 ⑧） |

> ⚠️ **产品文案仍然要写清楚频率差异**（与"不进宇宙"的旧结论相比，只是措辞变了）：
> **"自定义板块里的票会同步行情，但刷新频率低于自选池（约 15 分钟级）；
> 想要秒级行情请把它加进自选池，或直接点开该板块查看（点开后即为实时）。"**
> 不说清 = 用户以为"进了板块就是实时"，实际是 15 分钟前的价 —— **那是会误导交易的**。

#### ⑤b 🔑 池级前台：**"一个用户最多同时只有一个池要实时刷新"**

> **需求方原话（2026-09）**："当用户切换到自定义板块查看时，
> 原来的自选股实时刷新可以切到用户当前查看的自定义板块实时刷新，
> 也就是**一个用户最多同时只有一个股池要实时刷新**。"

**这条是本设计里最关键的一处约束，因为它把"宇宙"和"实时"解耦了：**

```
宇宙（Universe）        = 所有用户所有池的并集   → 决定"缓存要多大"  （≈3,500 只）
实时集（Live Set）      = 每个用户当前查看的那个池 → 决定"要打多少请求"（≈100~200 只）
```

**如果没有这条约束**，会被迫二选一，两条都是错的：

| 错误做法 | 后果 |
|---|---|
| 所有池都实时刷 | 100 VIP × 21 个池（5 自选 + 20 板块）× 每池 20~200 只 = **数万只实时** → 数据源必被限流 |
| 只有自选池实时、板块永不刷 | 用户切到板块看到的是**十几分钟前的价**，而他以为在监控 |

**有了这条约束**：**实时集只跟"当前在看什么"有关，跟"他有多少池"无关**——
100 个用户 → 最多 100 个活跃池，而不是 2,100 个。

#### ⑤c 池的刷新档位（三档，由"是否当前查看"与"池类型"共同决定）

| 档 | 什么池 | 报价 | 分时 | 5mK | 谁在刷新 |
|---|---|---|---|---|---|
| **P0 当前池** | 用户**正在看**的那个池（自选或板块）**里的全部票** | **5s** | **60s** | 120s | 该用户的 WS 连接驱动 |
| **P1 背景池** | 用户有但**没在看**的自选池 | **5s**（报价是批量，几乎免费） | 300s | 300s | 全局宇宙循环 |
| **P2 冷池** | 用户有但**没在看**的自定义板块 | **5s**（同上） | **900s（15 分钟）** | **1800s（30 分钟）** | 全局循环**有空闲时**才刷 |

**P2 的"有空闲才刷"怎么实现**（不是嘴上说说）：

```python
# 全局宇宙刷新循环里的优先级调度（伪码）
async def refresh_loop():
    while True:
        budget = await capacity_budget()          # ① 当前可用余量
        #   budget 由三部分算出：令牌桶剩余 + 并发闸门空闲数 + 上一轮耗时占空比
        await refresh_tier("P0", budget)           # ② P0 无条件刷（用户盯着）
        await refresh_tier("P1", budget)           # ③ P1 有余量就刷
        if budget.slack_ratio > 0.5:               # ④ P2 只在余量 >50% 时刷
            await refresh_tier("P2", budget, limit=budget.slack // cost_of_one)
        await sleep_until_next_tick()
```

> **为什么 P2 用"余量门控"而不是"固定 15 分钟定时"**：
> 固定定时会在**开盘洪峰时**准时开跑 P2 —— 那正是最不该抢资源的时候。
> 余量门控让 P2 **自动避峰**：洪峰时 slack 低 → P2 停；
> 10:30 之后用户散了 → slack 高 → P2 补刷。**这是"错峰"最省事的实现**。

**切换时的即时升级**（用户切到板块 → 该板块立刻进 P0）：

```
用户点开"AI概念"板块
  → 前端发 {type:"active_pool", pool_id:"sec_ai"}
  → 服务端：该池的票从 P2 档**立即提升到 P0 档**
  → 若缓存里的分时数据已过期（>60s），**单飞取一次**并写入
  → 用户体验：切过去时列表先显示已有报价（命中 L0），
     1~2 秒内分时补齐（后台取数），**不会白屏等**
  → 同时：他原来的自选池从 P0 降回 P1（不是停止，是降档）
```

> ⚠️ **细节：切换不能"清空再加载"**。现有前端已有"按 code 缓存快照"避免白屏的机制
> （`INTRADAY_T_DESIGN.md` §4.13 第 4 条），**池级切换要复用同一套**：
> 先用缓存铺出来，再静默补齐。否则"切池"会变成"每次等 2 秒"。

#### ⑥ 存储重算（多池 + 自定义板块）

| 数据 | 规模（S2：100 VIP + 300 试用） | 大小 |
|---|---|---|
| 自选条目 `dim_user_watchlist` | 17,600 条 | ~3MB |
| 自选池 `dim_user_pool` | 100×5 + 300×5 = **2,000 行** | < 1MB |
| 个性化参数 `dim_intraday_profile_v2` | 最坏 400 人 × 各自上限（100 / 25）= **17,500 行** | 每行 0.5~2KB → **约 35MB** |
| 自定义板块 `dim_quant_sector` | 100×20 + 300×3 ≈ **2,900 行** | ~1MB |
| 板块成员 `map_quant_sector_stock` | 按**实际填写**估 2,900 个板块 × 均 40 只 = **≈12 万行** | ~10MB |
| **合计** | **≈16 万行 / < 50MB** | **仍不构成存储压力** |

> 对比：行情仓库 31GB。**用户侧数据小三个数量级** ——
> 所以"多池 + 自定义板块"在设计上**不需要任何特殊结构**，
> 需要的是**配额校验**（防绕过）与**刷新档位**（§7.2.6⑤c：哪些池实时、哪些降频）。
>
> ⚠️ **板块成员是唯一会"长得比预期快"的表**（用户会往板块里塞票）。
> 所以 `sector_size_limit` 的校验**必须和自选上限一样严**，且要有
> "当前 12 万行 / 上限"的监控 —— 这条表的上限是**产品决策**，不是技术限制。

#### ⑦ 对 §8.5.4 容量推演与 §7.2.6 护栏的修正

| 原值 | 新值 | 原因 |
|---|---|---|
| 宇宙按 **800 只**设计 | 按 **3,500 只**（自选 2,000 + 板块 1,500） | ① 多池自选；② 板块进宇宙（降频） |
| 长尾降级阈值 **1,500 只** | **800 只进 H 档**（阈值成为"H 档容量"而非"总宇宙上限"） | 3,500 只里大半是 L 档 |
| 分时档 TTL **60s（全体）** | **60s（仅 P0 当前池）/ 300s（P1 亮背景自选）/ 900s（P2 冷板块）** | 全宇宙 60s → 58 只/s（饱和） |
| **报价按层级分档** | **全宇宙统一 5s**（取消分档） | 批量接口一次 300~500 只、耗时几乎不随数量增长（ADR D20）|
| 内存 < 500MB | 宇宙缓存 **≈115MB** | 3,500 只 × 33KB |
| 结论"数据源不是瓶颈" | **仍然成立，余量约 10×** | 合计 ≈13 req/s vs 能力 ~130 req/s |

#### 7.2.7 多池与板块进宇宙后的容量结论

- §7.1 讲的是**计算**共享（因子层 L0 + 口径指纹）；本节讲的是**取数**共享（活跃宇宙）。
  两者是同一原则的两面：**"与用户无关的东西只做一次"**。
- §8.5.2 第 5 条（缓存击穿放大）在本节的具体形态就是"Redis 挂了之后 100 个用户
  同时冷取 3,500 只票" → 必须 fail-closed 限流，而不是把数据源打穿。
- **刷新档位不按付费层级分**（报价全层级统一 5s），而是按
  **"是否当前查看"（P0/P1/P2）+ "接口稀缺性"（报价 vs 逐只）** 分 ——
  这是 ADR D20 确立的原则：**节流要打在稀缺资源上，而不是打在用户身上**。

### 7.3 重任务队列（与实时链路物理隔离）

`fact_job` 表 + 独立 worker 进程（**注意不要和 §7.2 的取数宇宙混在一起**：
宇宙是"取数"，这里是"算"，两者共用的是同一套并发闸门与优先级思想）：

```sql
CREATE TABLE fact_job (
    job_id     TEXT PRIMARY KEY,
    tenant_id  TEXT NOT NULL,
    user_id    TEXT NOT NULL,
    kind       TEXT NOT NULL,        -- level_fit/backtest/selector_run/crowding_refresh/
                                     -- mainline_sync/model_train/report_generate
    priority   INTEGER NOT NULL DEFAULT 5,   -- 交互式(level_fit)=1，批量(选股)=7
    status     TEXT NOT NULL,        -- queued/running/succeeded/failed/cancelled
    payload_json TEXT NOT NULL DEFAULT '{}',
    dedup_key    TEXT NOT NULL,      -- 幂等键：kind + 参数指纹（§8.5.2 第 4 条）
    result_ref   TEXT,
    error        TEXT,
    requested_at TEXT NOT NULL,
    started_at   TEXT,
    finished_at  TEXT,
    worker_id    TEXT
);
CREATE INDEX idx_job_queue ON fact_job(status, priority, requested_at);
CREATE UNIQUE INDEX uq_job_dedup ON fact_job(dedup_key) WHERE status IN ('queued','running');
```

- **租户配额（三段式）**：`(tenant, user)` 并发上限 + 每任务 CPU/内存/时长限额
  + 公平调度队列；超限直接 429。
- **交互式优先**：`level_fit` priority=1，全市场选股 priority=7。
- **背压**：队列深度 > N 时新任务返回 503 + 预计等待（不无限堆积）。
- **领取原子化**：`SELECT ... FOR UPDATE SKIP LOCKED`（Postgres）。

### 7.4 WS 推送的"按用户"模型

```
WS 连接建立 → 鉴权 → 注册 (tenant_id, user_id, code)
服务端推送循环（全局唯一，1s tick）：
  ├─ 对每个"有订阅者的 (code, 口径指纹)"检查版本号
  ├─ 版本变了 → 取缓存 payload → 广播给订阅该 key 的所有连接
  └─ 自选列表：按用户版本号推（A 的池子变了只推 A）
```

配套：连接数上限/用户、心跳、慢消费者丢弃（否则一个卡住的浏览器拖住广播循环）。

### 7.5 远程访问：Cloudflare Tunnel（对外发布方案）

> **需求原文**："需要实现通过 Cloudflare Tunnel 实现远程共享给用户网站。"
> 这是**本项目 §2.2 H6（只绑 127.0.0.1、无法远程访问）的正解**，
> 而且它顺带解决了一个安全难题：**不用开公网入站端口、不暴露服务器真实 IP**。

#### ① 拓扑与本项目的落点

```
用户浏览器 ──HTTPS──► Cloudflare 边缘（TLS 终结 / WAF / 限流 / 可选 Access）
                            │  加密隧道（cloudflared **主动外连**，无入站端口）
                            ▼
                     cloudflared（同机进程）
                            │  http://127.0.0.1:8100
                            ▼
                     uvicorn src.api.main:app  ← **保持 127.0.0.1 绑定不变** ✅
```

**为什么这条方案特别契合本项目**：现有启动命令是
`uvicorn src.api.main:app --host 127.0.0.1 --port 8100`（§2.2 H6 实测 cmdline）。
`cloudflared` 与 API **同机运行**时，它访问 `127.0.0.1:8100` 完全没有障碍 ——
**所以"只绑 localhost"从缺点变成了优点：机器上没有任何端口对公网开放。**

```yaml
# ~/.cloudflared/config.yml
tunnel: moss-finagent
credentials-file: /etc/cloudflared/<tunnel-id>.json
originRequest:
  # ★ WS 必须显式配置，否则做T的实时推送会被中断
  noTLSVerify: false
  connectTimeout: 30s
  tcpKeepAlive: 30s            # 保持长连接
  keepAliveTimeout: 90s
  disableChunkedEncoding: false
ingress:
  - hostname: moss.example.com
    service: http://127.0.0.1:8100
  - service: http_status:404   # 兜底：未匹配的域名一律 404
```

#### ② 六个必须处理的坑（每条都会真的发生）

| # | 坑 | 后果 | 处置 |
|---|---|---|---|
| 1 | **WS 长连接被中间层掐断** | 做T推送"偶尔断"，用户看到"轮询模式"徽标 | `tcpKeepAlive: 30s`；**应用层每 25s 发心跳帧**（现有告警 WS 已有 `_hub_keepalive`，做T WS 要补）；客户端指数退避重连（§8.5.2 第 1 条） |
| 2 | **真实客户端 IP 丢失** | 审计里全是 Cloudflare IP；按 IP 限流/防刷失效 | 读 `CF-Connecting-IP`（**只信任来自 Cloudflare 的请求**）；落 `fact_access_audit.ip`；**不要用 `X-Forwarded-For` 的第一段**（可伪造） |
| 3 | **"隧道=内网"的错误假设** | 以为有反代就不用鉴权 | ⚠️ **隧道不提供任何认证**。公网任何人访问 `moss.example.com` 就到达你的 API → **`MOSS_TENANCY_ENFORCE=1` 与 `MOSS_ALLOW_HEADER_IDENTITY` 关闭是硬要求**（§8.7.2 的启动自检已覆盖这条） |
| 4 | **`X-Internal-Call` 之类的内网信任头被伪造** | 越权（借鉴自 `moss-finance-assistant` 的教训：**网关必须剥离外部传入的这些头**） | cloudflared 不做头剥离 → **应用层必须自己剥**：在 `TenancyMiddleware` **最前面**清掉 `X-Internal-Call` / `X-Tenant-Id` / `X-User-Id` / `X-Roles`（除非显式开发开关） |
| 5 | **Cloudflare Access 开了却把 API 挡死** | 前端拿不到 API，或 OAuth 回调循环 | Access 保护 **HTML 入口**、放行 `/api/*`（由应用层 token 管），或干脆不用 Access 只靠应用层鉴权（**二选一，别混**） |
| 6 | **出口流量计费与限速** | 免费版对 WS/大流量有限制，且商业使用条款需评估 | 出网带宽实测 < 6 Mbps（§7.2.6③）；但 **WS 帧频率高**（100 连接 × 5s × 1.2KB）会产生**大量请求数** → 关注 Cloudflare 的请求计费口径与 Fair Use 条款 |

#### ③ 安全清单（发布前逐条打勾，与 §8.7.2 呼应）

| 项 | 要求 |
|---|---|
| 绑定 | uvicorn 保持 `127.0.0.1`；**不要为了 cloudflared 改成 `0.0.0.0`**（同机进程不需要） |
| 鉴权 | `MOSS_TENANCY_ENFORCE=1`；`MOSS_ALLOW_HEADER_IDENTITY` **必须为空** |
| 身份头 | 中间件最前面**剥离** `X-Internal-Call`/`X-Tenant-Id`/`X-User-Id`/`X-Roles` |
| TLS | 由 Cloudflare 终结；**源站不回源明文到公网**（隧道是加密的，符合要求） |
| WAF / 限流 | Cloudflare 侧规则（防刷）**+** 应用侧每用户配额（§4.6）——**两层都要有** |
| 审计 | `fact_access_audit` 记 `CF-Connecting-IP`；**审计里要能区分"来自隧道"与"来自本机"** |
| 健康检查 | `/healthz` 保持公开（Cloudflare 健康检查与探针用），**但不得泄露内部细节** |
| 部署 | `cloudflared` 作为**系统服务/容器**常驻（`restart: unless-stopped`），与 API 进程解耦 —— **它挂了不应影响本机使用** |

> **一句话**：*"Cloudflare Tunnel 解决的是**怎么让外面访问进来**，
> 不解决**谁可以进来** —— 后者仍然完全靠应用层的 `MOSS_TENANCY_ENFORCE` + 令牌 + 能力码。"*
> 把这两件事混起来的部署，是"以为有隧道就安全了"的典型。

### 7.6 健壮性：模块隔离、降级矩阵与"绝不整站崩"

> **需求原文**："子功能异常崩溃时，系统不要全崩溃；数据部分没获取到时自动换数据源；
> 如果数据无论怎样都获取不到，就上报数据源异常。"

这一节回答的是**故障隔离的粒度**。核心问题不是"有没有 try/except"，
而是：**一个下游挂了，最大的爆炸半径是多大？**

#### 7.6.1 现状盘点：哪些"已经隔离"，哪些是"整站级"隐患

| 层 | 现状 | 隔离程度 |
|---|---|---|
| 数据源容灾 | `IntradayDataProvider` 有三级链（腾讯 → 新浪 → 东财 / QMT），带 `source_health` EWMA 排序 + 冷却 | ✅ 已隔离（但见 §7.6.3 的两个缺口） |
| 子链路取数 | `service.snapshot` 用 `asyncio.gather(..., return_exceptions=True)` + `take()` 逐个兜异常，失败进 `gaps` | ✅ 已隔离，**设计是对的** |
| 模块间 | 各 API 路由独立；`main.py` 里事件告警/权重档案/量化选股**建表失败只降级自己** | ⚠️ 靠"每处都写对"，**没有机制保证** |
| **进程级** | **全模块跑在同一个 uvicorn 进程**（`api/main.py`）；`quant_select`（50KB）、`mainline` 回测、`crowding` refresh 都在里面 | ❌ **整站级隐患**：一次 OOM / 一次未捕获的段错误 = 100 人全断 |
| **历史事故** | `xtquant` 并发下载导致进程**无 traceback 猝死**（`INTRADAY_T_DESIGN.md` §4.9） | ❌ 已发生过一次整站崩溃 |

#### 7.6.2 隔离设计：两层（模块级 + 状态级）

**第一层：模块级隔离（进程/服务边界）**

| 措施 | 做法 | 收益 |
|---|---|---|
| **进程拆分** | §8.2 的 `api / realtime / worker / scheduler / qmt-sidecar` | 一个模块崩不带走另一个；worker 崩了实时链路照常 |
| **模块独立降级开关** | 每个模块一个 `MODULE_<name>_ENABLED` 开关 + 启动时按 `SERVICE_ROLE` 裁剪 | 出问题的模块可**单独摘掉**，其余继续服务 |
| **重活移出 API 进程** | §7.3 的 `fact_job` 队列 | 全市场选股（实测 27s，撞预热 828s）不再拖死交互 |
| **危险调用子进程化** | 沿用现有 `download_history_isolated`（QMT 下载放子进程，原生崩溃只死子进程）；**LLM 生成的连接器**同样子进程 + 超时 | 把"会崩的东西"关在笼子里 |
| **全局异常兜底** | `sys.excepthook` + `asyncio` 异常处理器 + uvicorn `--limit-max-requests`（定期重启防内存泄漏） | 兜住"没人想到的那条路径" |

**第二层：状态级隔离（Actor 模式，参考 `moss-finance-assistant`）**

现有代码里最危险的一类 bug 来自**共享可变状态**（§2.2 H2 的 30 个缓存字段、
`_watch_lock`、`_watch_generation`）。兄弟项目 `D:\code\moss-finance-assistant`
用 Actor 模式系统性地解决了这个问题，值得直接借鉴：

```python
# 借鉴自 moss-finance-assistant/shared/actors/actor_base.py（思路，不是照抄代码）
# next_state = f(current_state, input)   —— 纯函数状态转换
class Actor:
    _mailbox: asyncio.Queue[Envelope]     # 外部只能投消息，不能碰状态
    __state: T                            # 私有状态

    async def _run_loop(self):
        while True:
            env = await self._mailbox.get()
            try:
                new_state, response = await self._safe_handle(env)
                self.__state = new_state               # 唯一修改点
            except Exception as exc:
                # ★ 单条消息失败不影响循环 —— 这就是"故障隔离"
                log.error("处理 %s 失败: %s", env.msg_type, exc)
```

**为什么比裸 `asyncio.Lock` 好**（这段在面试里很值钱）：

| 裸锁 | Actor |
|---|---|
| 只解决"不冲突"，**不解决"修改点散布各处"** | 状态修改**收敛到 `handle_message` 一个函数** |
| 无法审计所有修改点（散在业务 catch/finally/回调里） | 每个状态转换都有 `input → output` 边界，**可重放、可回溯** |
| 异步回调（`done_callback`）仍能偷偷改状态 | 外部**没有**改状态的路径 |
| 一处锁内抛异常可能让持有者状态半更新 | 返回新状态，**旧状态不被就地破坏**；单条消息失败不影响循环 |

**在本项目的落点**（把 30 个字段里的哪些 Actor 化）：

| 候选 | 为什么适合 | 优先级 |
|---|---|---|
| `SourceHealthTracker`（EWMA + 冷却） | 多协程并发 `record_failure/success` 会互相交错，导致排序抖动 | **P1**（做T主链路依赖它） |
| `_watch_cache` + `_watch_generation` | 现有 `_watch_lock` 只在单进程有效，且 generation 更新有竞态 | **P1** |
| 信号推送去重（`cooldown_minutes`） | 多用户下按 `user_id` 分片后，仍需保证同用户同标的只推一次 | **P2** |
| SLO/指标累计器 | 计数器被并发更新会丢数 | P2 |
| LLM 网关熔断器 | 现有 `circuit_breaker.py` 已是独立模块，Actor 化收益中等 | P3 |

> ⚠️ **不要全盘 Actor 化**：Actor 的代价是**所有访问异步化 + 邮箱可能积压**。
> 只对"**被多协程并发写、且写坏了会导致业务错误**"的状态用它。
> 纯只读缓存（`_character_cache`、`_chip_cache`）保持现状即可。

#### 7.6.3 数据源自动切换：现状已做，但有两个缺口

**已实现（做T链路，见 `sources.py` / `source_health.py`）**：
三级容灾链 + EWMA 延迟排序 + 失败冷却 300s + `should_attempt` 准入 + 探索性重试。

**缺口 ①：冷却按 `(source, method)` 记录，多用户下会放大成 N 次失败探测**

```python
# 现状：cooldown key 是 (source, method)，且强制刷新会清掉冷却
self._cooldown_key(source, "trend")     # 例如 ("tencent", "trend")
```

问题：腾讯的分时接口挂了，通常**同一域名的其它接口也一起挂**（网络/限流是域级现象）。
但冷却只记在 `("tencent", "trend")` 上 → 报价/分钟K 各自再撞一遍超时。
100 个用户并发时，每个用户各触发一次 = 100 次白等。

**改法**：把冷却提升到**(来源, 域)粒度**，并提供"域级连带冷却"：

```python
SOURCE_DOMAINS = {
    "tencent":   "gtimg",       # qt.gtimg.cn / ifzq.gtimg.cn 同域族
    "sina":      "sina",
    "eastmoney": "eastmoney",
    "qmt":       "local-term",
}
# 连续 N 次（默认 3）失败 → 整个域进入冷却；单方法失败仍只冷却该方法
```

**缺口 ②：失败只进 `gaps` 文本，没有"上报"**

现状：取数失败 → `health.gaps.append("...")` → 前端显示一段文字。
需求要的是"**上报数据源异常**"，意味着**可聚合、可告警、可统计**：

```sql
-- 数据源异常上报（只追加，不更新）
CREATE TABLE fact_source_anomaly (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL,              -- 发生时刻
    trade_date  TEXT NOT NULL,              -- 交易日（可回放）
    source      TEXT NOT NULL,              -- tencent / sina / qmt / eastmoney / llm
    domain      TEXT NOT NULL,              -- gtimg / local-term / ...
    method      TEXT NOT NULL,              -- quote / treand / bars / board
    code        TEXT NOT NULL DEFAULT '',   -- 哪个标的（可空 = 全局）
    kind        TEXT NOT NULL,              -- timeout / http_5xx / parse / empty / blocked
    detail      TEXT NOT NULL DEFAULT '',
    retry_after TEXT,                       -- 冷却到什么时候
    -- 聚合成"事件"用（同一 (source,method,kind) 在窗口内合并计数）
    fingerprint TEXT NOT NULL
);
CREATE INDEX idx_anomaly_src_at ON fact_source_anomaly(source, at);
CREATE INDEX idx_anomaly_fp     ON fact_source_anomaly(fingerprint, at);
```

配套三条（否则"上报"没用）：

| 机制 | 做法 |
|---|---|
| **聚合** | 同一 `fingerprint` 在 5 分钟窗口内合并成一条，带 `count`（否则 100 用户并发刷出 100 条重复记录） |
| **告警** | 某 source **全部方法**在 10 分钟内失败率 > 50% → 触发运维告警（复用现有事件告警通道）；前端顶部横幅"行情源 X 异常，已切换 Y" |
| **降级可见** | 快照的 `health.gaps` 保留人话说明（现有），但**额外带上 `anomaly_id`**，面板可点进去看详情 |

**"无论怎样都取不到"的最终行为**（三层，与项目既有纪律一致）：

```
① 链上所有源失败 → 记 fact_source_anomaly（含全部 attempts）+ gaps 写明"全链失败"
② 绝不回退 simulated（既有硬约束，保留）→ 数据置 None，前端显示 "—" 而不是假数字
③ 上报：运维告警 + 前端横幅；用户在面板上看到的是"该维度不可用（原因）"，
   且该因子从有效权重中扣除（现有 available_weight 机制，保留）
```

> **一句话**：**宁可让用户看到"这次没数据"，也不能让他看到一个编出来的数字。**
> 这条是项目在 `README.md` 里已经确立的取舍，多用户化不改变它。


---

## 八、部署演进

### 8.1 阶段 A：单实例 + 用户维度贯通

```
浏览器 ──HTTPS──> Nginx/Caddy ──> uvicorn (1 副本, **单 worker**；原因见下)
                                     └─ 全部模块 + 调度
Postgres（多租户表 + RLS） + Redis（L0 缓存 / 单飞锁 / 会话）
```

- **`uvicorn --workers N` 不是免费的多副本**：`qmt_guard._QMT_LOCK` 是
  `threading.RLock()`（进程级，`src/core/qmt_guard.py:52`），多 worker 各自持锁
  → QMT 并发访问复活（历史上把服务**无 traceback 猝死**，见 `INTRADAY_T_DESIGN.md` §4.9）。
  → **阶段 A 只能 1 worker**；若确需多 worker，QMT 必须走**独立 sidecar**。
- 此阶段**不解决容量**，只解决正确性；60 人以内够用（`设计推演`）。
- 必须做：`MOSS_TENANCY_ENFORCE=1`、前端带 token、CORS 白名单、基础速率限制。

### 8.2 阶段 B：进程拆分（按资源特征，不按人数）

| 进程 | 职责 | 副本 | 为什么单独 |
|---|---|---|---|
| `api` | HTTP/WS 接入、鉴权、读缓存、轻计算 | 1~2 | 可水平扩；必须能随时杀 |
| `realtime` | 做T快照循环、报价快车道、信号推送 | **1** | 有全局定时循环与推送，多副本会重复推送 |
| `worker` | `fact_job` 消费：拟合、回测、选股、同步、训练 | 1~3 | CPU/IO 重，绝不能和实时抢 |
| `scheduler` | CronScheduler（**仅此进程**） | **1** | 多副本 = N 倍全市场写入 |
| `qmt-sidecar`（可选） | 独占 xtquant 句柄 | **1** | 见 §8.1 |

- 进程间通过 **Redis**（缓存/锁/发布订阅）与 **Postgres**（状态）协作。
- `src/api/main.py:214` 的 lifespan 要按 `SERVICE_ROLE` **条件启动**各子系统（现在一把全启）。
- `connectors/router.py` 的 per-key lock、`intraday/sources.py` 的单飞锁
  **必须换成 Redis 锁**，否则跨进程失效。

### 8.3 31GB 行情仓库的共享问题（容易漏掉）

`data/quant` 实测 31.26GB（`warehouse.db` 14.35GB + `warehouse.ready.db` 14.4GB +
`tushare/` 2.34GB），另有 `data/archive` 3.41GB、`data/backtest` 1.12GB。

| 方案 | 适用 | 代价 |
|---|---|---|
| **只读共享卷**（推荐先用） | 多进程同机：全部只读打开，写由 `quant_data_sync` 单进程 | 需严格区分读写副本（项目已有 `warehouse.ready.db` 这个"就绪副本"模式，沿用） |
| NFS/SMB 共享盘 | 多机 | SQLite over NFS **不可靠**（锁语义），只读尚可，写入必坏 |
| 迁 Postgres/TimescaleDB | 长期 | 14GB → 需评估导入与查询性能；`fact_data_points` 281 万行其实不大 |
| 列存（Parquet/DuckDB） | 长期最优 | 因子面板扫描快 1~2 个数量级，但要改 `quant/warehouse.py`(69KB) 读写路径 |

**本期建议**：阶段 A/B 用"只读共享卷 + 单写者"，把仓库当 **L0 外部资源**。

### 8.4 分布式互斥（必须做，否则多进程会重复干活）

| 锁 | 键 | 现状 |
|---|---|---|
| 定时任务单次执行 | `lock:scheduler:<job>` | 无（进程内 Cron） |
| 行情同步 | `lock:quant_data_sync` | 无 |
| 全市场选股跑批 | `lock:selector:<trade_date>` | 无 |
| 单飞取数 | `lock:fetch:<code>:<kind>` | 进程内 `_locks`（`sources.py:1143`） |
| 单飞快照 | `lock:snap:<code>:<口径指纹>` | 无（§7.1 的核心） |
| 竞价出池 | `lock:auction:pool:<trade_date>` | 无 |
| 推送循环单例 | `lock:realtime:loop` | 无 |
| 用户写自选 | **不需要全局锁** | 现在有 `_watch_lock` → 改用行级乐观锁/`updated_at` 比对 |

Redis 锁必须带 **TTL + owner token + 续租**，释放走 Lua 脚本比对 owner
（**不要自研分布式锁** —— 最容易写出"释放了别人的锁"）。

### 8.5 分布式扩展与故障面（**目标态设计，未实现**）

> ⚠️ **本节是设计推演，不是已实现**。价值在于：把"多副本"会引入的
> **每一个新故障模式**事先列出来，并给出判断依据。

#### 8.5.1 目标态拓扑

```
                     ┌──────────── Nginx / Caddy（TLS、限流、WS 反代）────────────┐
                     │  路由：HTTP 轮询；WS 按 code 一致性哈希粘住同一副本          │
                     └───────┬──────────────────────────────┬────────────────────┘
                    ┌────────▼────────┐            ┌────────▼────────┐
                    │  api #1         │            │  api #2        │   可 1..N
                    └────────┬────────┘            └────────┬───────┘
        ┌────────────────────┼──────────────────────────────┼────────────────────┐
┌───────▼────────┐  ┌────────▼─────────┐          ┌─────────▼────────┐  ┌────────▼────────┐
│ realtime (×1)  │  │ worker (×1..3)   │          │ scheduler (×1)   │  │ qmt-sidecar (×1)│
│ 快照循环/推送   │  │ fact_job 消费     │          │ Cron 唯一执行     │  │ 独占 xtquant    │
└───────┬────────┘  └────────┬─────────┘          └─────────┬────────┘  └────────┬────────┘
        └────────────────────┼──────────────────────────────┼────────────────────┘
                    ┌────────▼────────┐            ┌────────▼────────┐
                    │  Redis          │            │  PostgreSQL     │
                    │ 缓存/锁/发布订阅  │            │ 状态 + RLS      │
                    └─────────────────┘            └─────────────────┘
                              │
                    ┌─────────▼──────────┐
                    │ 行情仓库（只读共享卷）│ 31GB，单写者
                    └────────────────────┘
```

**进程数不是按"人数"算的，是按"资源特征"算的**（见 §8.2）。

#### 8.5.2 多副本引入的九个新故障（逐个给判断与处置）

| # | 新故障 | 为什么会发生 | 处置 |
|---|---|---|---|
| 1 | **WS 连接漂移**：用户粘在副本 A，A 重启 → 100 个连接同时重连冲击副本 B | 负载均衡不保证粘性 | 客户端**指数退避 + 抖动**；服务端握手限流；WS 心跳 + `1012 Service Restart` 主动告知 |
| 2 | **信号推送重复**：两个副本都在跑快照循环 | 循环未单例化 | 推送循环**只允许 realtime×1 持有** `lock:realtime:loop`（TTL 续租） |
| 3 | **权限缓存跨副本不一致**：A 认为有权限，B 已收权 | `user:permissions:*` TTL 10 分钟 | **不要靠"等 TTL 过期"**：变更 → Redis 广播失效 + **权限版本号**，请求携带版本号，副本发现落后即视为**未授权**（fail-closed）。这正是 Zanzibar **zookie** 要解决的 **new-enemy problem**（§2.6）。QuantMind 的 10 分钟 TTL 是可改进点 |
| 4 | **计算重复调度**：两个 worker 抢同一 job 各算一遍 | 队列未原子领取 | `SELECT ... FOR UPDATE SKIP LOCKED`；任务**幂等**（`dedup_key` + 结果按参数指纹去重） |
| 5 | **缓存击穿放大**：Redis 抖动 + L1 同时过期 → 全打到公网数据源 | 两级缓存同时失效 | L1 TTL 加**抖动**（±10%~20%）；Redis 层单飞；Redis 不可达时**降级为进程内单飞并限流**（fail-closed，而不是 fail-open 打数据源） |
| 6 | **授权判定依赖 DB**：每请求查权限 → DB 成瓶颈 | 权限数据在 DB | 权限集进 Redis（`user_id` + 版本号），变更时版本号自增；**请求路径不查 DB** |
| 7 | **RLS 会话变量与连接池串号** | 连接复用未 RESET | 见 §5.4 第 1 条（事务级 `SET LOCAL`）+ 单测断言 |
| 8 | **定时任务 N 倍执行**：3 个副本都跑 `quant_data_sync` | 进程内 Cron | 只在 scheduler×1 注册（`SERVICE_ROLE`）+ 分布式锁双保险；任务自身**幂等**（按 `trade_date`） |
| 9 | **令牌无法吊销**：HMAC 无状态签名的代价 | 现实现无过期/吊销 | 加 `fact_session` + `jti` 黑名单（Redis，TTL=剩余寿命）；"登出/收权立即生效"必须靠黑名单 |

#### 8.5.3 一致性等级：不追求强一致，但要写清"哪里可以不一致"

| 数据 | 一致性要求 | 理由 |
|---|---|---|
| 行情/因子（L0） | **最终一致**（容忍秒级） | 差 1~2 秒不影响做T决策，但**差一天**才致命 → 用 `trade_date` 钉死而非时钟 |
| 用户自选/权重（L2） | **读己之写** | 用户刚改完必须立刻看到；跨副本：写后把该用户**粘回同一副本**，或失效广播 + 版本号比对 |
| 权限/能力集 | **强一致（收权方向）** | 收权必须立刻生效（审计事故）；赋权可延迟几秒。实现 = 版本号 + 广播失效（zookie 思想） |
| 审计链 | **追加一致 + 不可篡改** | 多副本写同一 JSONL 会交错 → **只由中间件所在的 api 进程追加**，或用 DB 序列 + 哈希链 |
| 定时任务 | **单次执行**（at-most-once + 幂等） | 宁可漏一次可补偿，不可重复写全市场 |

> **一句话**：*"我不追求全局强一致，但每一类数据的不一致窗口都是**显式声明**的，
> 且收权与审计这两处向强一致靠。"*

#### 8.5.4 容量推演（**取数为实测，计算为设计推演**）

前提：§7.1 的计算共享改造完成 + §7.2 的活跃宇宙与**三档分层**生效。

**参数设定**（对齐最新需求：VIP 300 人 × **5 池 / 100 只**、试用 300 人 × **5 池 / 25 只**、
管理员 1 人 × 100 只）→ **未去重自选条目 ≈17,600**，
**去重后宇宙 = 2,000 只**（🔒 需求方 2026-09 确认；最坏情景 3,000 只作为**压测上限**）。

> ⚠️ **这一节已两次重算**：先是"800 只"→"3,000 只"（发现多池自选），
> 再按需求方确认收敛到 **2,000 只**。保留这段说明是为了让读者知道哪些数字被修正过。

**（A）数据源压力 —— 实测外推，按 F/H/L 三档**

| 数据 | 单只成本（`实测`） | 档位与规模 | 稳态速率 | 折算单核 |
|---|---|---|---|---|
| **报价**（批量，**不分档**） | **一次 300 只 / 98ms** | **全宇宙 2,000 只 @5s** | **1.4 req/s** | **≈0.9%** |
| **分时**（逐只） | 9.3KB / 80ms | F 100@60s + H 800@300s + **L 不刷** | **4.3 只/s** | 单并发 0.34s/s；**16 并发 ≈ 2%** |
| 5mK（逐只） | 24KB / 100ms | F 100@120s + H 800@300s + L 1,100@900s | **4.7 只/s** | 单并发 0.47s/s；**16 并发 ≈ 3%** |
| 常驻内存（宇宙缓存） | 33KB/只 | 2,000 只 | —— | **≈66MB** |
| **合计** | | 峰值 ×1.5 余量 | **≈10 请求/s、< 0.5 核、< 4 Mbps** | **vs 实测能力 ~130 req/s → 余量 ≈13×** |

**三处必须点明的判断**：

| 判断 | 依据 |
|---|---|
| 🔴 **"全宇宙分时 @60s"是不可行的** | `2,000 ÷ 60s = 33 只/s` → 单并发 **2.7s 墙钟/s**（16 并发 17%，**无余量**）；若宇宙涨到 3,000 只则 50 只/s → 4.0s/s（饱和）。所以分时档**只能给前台**，热门后台 300s、长尾不刷 |
| 🟡 **"在自选里" ≠ "要实时刷"**（除报价外） | 100 只自选里，用户同一刻只看 1 只；其余 99 只的**分数** 60~300s 更新足够。**报价**例外 —— 它是批量公共数据，全量 5s（§7.2.6④b） |
| ✅ **自定义板块不进宇宙** | 若进，2,000 只会变 10,000+，直接打穿。**必须写进产品规则并让用户看见**（§7.2.6⑤） |

**（B）服务端资源**

| 资源 | 400 人（100 VIP + 300 试用，S2）占用 | 推导 |
|---|---|---|
| 计算 CPU | 个性化打分 < 0.5 核 + 共享快照 0.15 核 | 见 §7.1；套餐分级（试用 180s）进一步降低 |
| 内存 | 进程 1.7GB（实测）+ 宇宙缓存 **105MB** + 每用户会话 ~10KB | 31.7GB 机器可容 4~6 个进程 |
| 出网带宽（下发） | 100 连接 × 50KB / 套餐间隔 | VIP 5s 档：≈ **2.7 Mbps**；试用 30s 档更低 |
| Redis | 行情/因子缓存 + 锁 + 会话 + 权限集 → **< 500MB** | 2,000 只 × 因子对象数 KB × 多周期 |
| 数据库 | 用户侧数据 **≈19 万行 / < 100MB**（§7.2.6⑥）；审计 ≈ 3 万行/日 | 对比行情仓库 31GB —— **小两个数量级** |
| **结论** | **单机（1 api + 1 realtime + 1~3 worker）仍然足够** | 需上第二台机器的信号：① 实时 p95 稳定 > 2s；② 重任务队列持续积压；③ 需不停机发布；④ **宇宙 >4,000 只**（此时 5mK 要降到 600s 档或加第二组数据源） |

> ⚠️ **取数那部分是实测外推，计算那部分是设计值**，别混说。
> 落地后要用 `scripts/_probe_datasource.py`（取数）与 `scripts/_probe_concurrency.py`（并发）
> 回填真实数字，并**替换掉这里的估算**。

#### 8.5.5 明确不做（本期的架构边界）

| 不做 | 理由 |
|---|---|
| K8s / 服务网格 | 1~3 台机器规模，编排层收益 < 运维与调试成本 |
| 消息队列（Kafka/RabbitMQ） | 任务量级"分钟级几十条"，Postgres 队列表 + `SKIP LOCKED` 足够；多一个中间件 = 多一个故障点 |
| 分库分表 | 个人数据 < 1 万行、行情 31GB 单机可放（D 盘余 293GB） |
| 跨机房容灾 | RPO/RTO 要求未到；先做**备份与恢复演练**（成本低、收益确定） |
| 微服务化 19 个 Agent | 与既有架构决策一致：职责分层而非 19 个服务 |
| 自研分布式锁 | 用 Redis 现成原语（`SET NX PX` + Lua 释放） |

### 8.6 账号与认证体系（注册 / 登录 / 找回 / 改密 / 记住我 / 手机号绑定）

> **需求原文**："要有用户注册、登录、改密码、记住密码、绑定手机号
> （通过手机号账号密码找回）的功能。"
>
> 这一节是**全系统风险最集中的一处**：它同时是**唯一的入口**、**最常被攻击的面**、
> 和**唯一的自救通道**。所以本节不只给功能，更给**每一处不能做错的地方**。
>
> **前置依赖**：**本轮已定：只用邮箱，短信能力后置**（🔒 2026-09，§8.6.8）。
> 所以**注册/找回/绑定的"验证通道"全部走邮箱**，短信相关设计保留为"将来可插拔"，
> **但本期不实现、不阻塞上线**。

#### 8.6.0 认证状态机（与 §8.6.1 的账号生命周期**正交**）

```
【账号生命周期】pending → active → disabled / expired / deleted   （§8.6.1，管理员驱动）
【本次认证状态】anonymous → authenticated → mfa_pending → locked  （本节，用户/风控驱动）
```

> ⚠️ **两个状态机必须分开存**，不要合成一个 `status` 字段：
> "账号是 active 但**本次**登录失败 5 次被临时锁定"是**两个维度同时成立**。
> 合成一个字段的结果是——临时锁定会把账号状态也改成 `locked`，
> 于是管理员在后台看到"这个 VIP 被禁用了"，而其实只是他输错了几次密码。

#### 8.6.1 账号审批流（**注册 ≠ 可用**）

> **需求原文（前一轮）**："用户注册要系统管理员审批才能通过。"

这条在**金融场景下不是可选项**：`MULTI_TENANCY_DESIGN.md` 引的
《证券公司信息隔离墙制度指引》第八条"**需知原则**"要求
"敏感信息仅限有合理业务需求或管理职责需要者知悉" ——
**谁能进系统本身就是需要审批的事**，不能自助开通。

##### 8.6.1.1 状态机（用户生命周期的七个态）

```
  ┌──────────┐  提交注册   ┌─────────┐  管理员批准  ┌────────┐
  │ (不存在) │ ─────────→ │ pending │ ─────────→ │ active │
  └──────────┘             └────┬────┘            └───┬────┘
                                │ 驳回                 │
                                ↓                      ├── 管理员禁用 ──→ ┌──────────┐
                          ┌──────────┐                 │                  │ disabled │
                          │ rejected │                 │   ←── 启用 ────  └────┬─────┘
                          └────┬─────┘                 │                       │
                               │ 修改资料重提            │ 到期（自动）           │ 软删除
                               └────→ pending          ↓                       ↓
                                                 ┌─────────┐            ┌───────────┐
                                                 │ expired │            │  deleted  │
                                                 └────┬────┘            │ (软删除)   │
                                                      │ 续期            └───────────┘
                                                      └──→ active
```

**七个态**：`pending`（待审）/ `active`（已启用）/ `rejected`（已驳回）/
`disabled`（已禁用）/ **`expired`（已到期）**/ **`deleted`（已软删除）**。

> ⚠️ **`expired` 与 `disabled` 必须分开**，因为**原因不同、恢复路径不同**：
> `disabled` 是**管理员主动禁用**（要人解封）；`expired` 是**有效期自然到期**（付钱续期即恢复）。
> 合成一个态会导致"到期用户找客服，客服看不出是欠费还是被封"。

```sql
ALTER TABLE dim_user ADD COLUMN status TEXT NOT NULL DEFAULT 'pending';
-- pending | active | rejected | disabled | expired | deleted
ALTER TABLE dim_user ADD COLUMN reviewed_by   TEXT;    -- 审批人
ALTER TABLE dim_user ADD COLUMN reviewed_at   TEXT;
ALTER TABLE dim_user ADD COLUMN review_note   TEXT;    -- 驳回原因（要能给人看）
ALTER TABLE dim_user ADD COLUMN applied_tier  TEXT;    -- 申请哪一档（trial/vip）

-- ★ 有效期（本次新增需求）
ALTER TABLE dim_user ADD COLUMN valid_from   TEXT;              -- 生效时间（NULL = 批准即生效）
ALTER TABLE dim_user ADD COLUMN valid_until  TEXT NOT NULL;     -- 【强制】不允许永久账号
ALTER TABLE dim_user ADD COLUMN expire_warned_at TEXT;          -- 到期预警已发（防重复提醒）
ALTER TABLE dim_user ADD COLUMN renewed_count INTEGER NOT NULL DEFAULT 0;

-- ★ 软删除（本次新增需求："管理员有删除权限"）
ALTER TABLE dim_user ADD COLUMN deleted_at   TEXT;              -- 软删除时间
ALTER TABLE dim_user ADD COLUMN deleted_by   TEXT;              -- 谁删的
ALTER TABLE dim_user ADD COLUMN delete_reason TEXT NOT NULL DEFAULT '';
-- 唯一索引必须**排除**已删除行，否则"删了就不能再用同一邮箱注册"
DROP INDEX IF EXISTS uq_user_email;
CREATE UNIQUE INDEX uq_user_email ON dim_user(email)
    WHERE email IS NOT NULL AND deleted_at IS NULL;
CREATE UNIQUE INDEX uq_user_username ON dim_user(username) WHERE deleted_at IS NULL;

-- 审批与生命周期流水（只追加，不可改）—— action 扩到覆盖全生命周期
CREATE TABLE fact_registration_review (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    at           TEXT NOT NULL,
    user_id      TEXT NOT NULL,
    action       TEXT NOT NULL,     -- approve | reject | disable | enable
                                    -- | expire | renew | delete | restore | tier_change
                                    -- | quota_change | role_change
    from_status  TEXT NOT NULL,
    to_status    TEXT NOT NULL,
    tier_code    TEXT NOT NULL DEFAULT '',
    valid_until  TEXT,              -- 变更后的有效期（续期审计要看）
    reviewer_id  TEXT NOT NULL,
    note         TEXT NOT NULL DEFAULT '',
    ip           TEXT,
    chain_hash   TEXT NOT NULL      -- 与访问审计同款哈希链
);
```

##### 8.6.1.2 有效期机制：**不能到点硬切断**

"每个用户都有使用有效期"这条需求，最危险的实现是"到点即登出"。
真实事故是：**用户正在盘中看盘，10:00 整突然被踢出去** —— 那比没有有效期更糟。

**分四段处理**（时间线）：

| 阶段 | 触发 | 行为 |
|---|---|---|
| **T-7 天** | 到期前 7 天 | 登录后弹窗 + 顶部横幅"账号将于 X 日到期，联系管理员续期"；同时给管理员一张"即将到期"列表 |
| **T-1 天** | 到期前 1 天 | 站内提醒 + 邮件（复用现有告警通道）；`expire_warned_at` 记录防重复 |
| **T-0（到期时刻）** | `valid_until` 到 | 状态置 `expired`。**已建立的 WS 连接不立即断**，但：① 拒绝**新的登录/新 WS 握手**；② 已连连接在**当个交易时段结束后**（或最长 30 分钟宽限）再断，并在前端显示"账号已到期（宽限中）" |
| **T+宽限后** | 宽限结束 | 正常断开；数据**保留**（不是删除）——续期后一切照旧 |

**三条必须做对**：

| # | 规则 | 为什么 |
|---|---|---|
| 1 | **`valid_until NOT NULL`** —— 不允许"永久账号"入库 | 否则总有人给自己开一个无期限账号；**永久的例外要走 break-glass 并留痕** |
| 2 | **判定在服务端每次鉴权时做**，不是只在登录时做 | 否则"登录后有效期到了还能一直用"（长会话场景） |
| 3 | **到期只冻结访问，不删数据** | 用户续期后期望"一切照旧"；且金融场景下**删除用户数据是合规事故**（见下） |

##### 8.6.1.3 删除：只能**软删除**，且**不删业务数据**

> **需求原文**："管理员有增加、删除权限。"
> 本项目的答案是：**能删，但删的是"访问权"，不是"数据"。**

**为什么不能在金融系统里硬删除用户**：

1. **审计链会断**。`fact_access_audit` 是哈希链（`chain_hash = H(前一条 hash ‖ 本条)`），
   删掉用户的审计记录会让链对不上 —— 而《证券法》**第二百一十四条**对
   "未按规定保存文件资料、篡改毁损资料"是有罚则的。
2. **留痕要求**：办法**第十六条**要求审计报告留存 **≥20 年**；
   《网络安全法》第二十一条要求网络日志 **≥6 个月**。**删用户 = 删他的操作痕迹**。
3. **持仓/分析结论的可追溯性**：他当时基于什么数据做的判断，事后（投诉、监管问询）要能还原。

**所以"删除"的语义定义为三级**，由管理员选择（默认第 1 级）：

| 级别 | 动作 | 效果 | 可逆 |
|---|---|---|---|
| **L1 停用**（默认） | `status='disabled'` | 不能登录，数据全留 | ✅ 一键恢复 |
| **L2 软删除** | `status='deleted'` + `deleted_at` | 从所有列表/统计中消失（默认过滤），**数据保留**；邮箱/用户名可复用（见上面的部分唯一索引） | ✅ 管理员可 `restore` |
| **L3 匿名化**（仅合规发起） | 把 `email`/`phone`/`display_name` 置为 `deleted-<hash>`，`user_id` **不变** | 满足"删除个人信息"，但**审计链与业务归属关系完整保留** | ❌ 不可逆（需四眼 + 合规审批） |

> **L3 是本项目推荐给"用户要求删除个人数据"场景的答案**：
> **匿名化 ≠ 删行**。用户的个人信息被抹掉，但"用户 u-123 在 9:31 看过 300308"
> 这条审计记录仍然成立且可验证 —— 这是金融系统与普通 SaaS 在删除语义上的**根本区别**。

**实现要点**：

```python
# 所有"用户列表/统计"查询必须默认带这个条件（否则已删除用户仍出现在界面上）
WHERE deleted_at IS NULL
# 仓储层统一封装（与 RLS 的 tenant_column_guard 同一风格：
# 未走封装的裸查询应当在 code review 里被拒）
```


#### 8.6.2 五条必须做对的细节（每条都对应一种真实事故）

| # | 细节 | 不做的后果 |
|---|---|---|
| 1 | **`pending` 用户不能登录**：`/auth/login` 在密码校验通过后**再查 status**，非 `active` 一律拒绝并返回明确原因 | 注册即可用 = 审批流形同虚设 |
| 2 | **注册接口要防刷**：同一 IP/邮箱/手机号限流 + 验证码；`dim_user` 的唯一索引先占位（防止并发注册同邮箱） | 被脚本灌 10 万个 pending 账号，管理员审批列表被淹没 |
| 3 | **审批是 break-glass 类动作**：`platform.user.manage` 能力码 + **四眼**（§4.3.1）；`reviewer_id ≠ user_id`（禁止自审） | 申请人给自己批 = 无审批 |
| 4 | **审批留痕进审计链**：`fact_registration_review` 哈希链 + 同时写 `fact_access_audit` | 合规查"谁在什么时候批了谁"时答不出来 |
| 5 | **驳回要能申诉/重提**：`rejected` 不是终态，允许修改资料后回到 `pending`（保留历史流水） | 一次误判就把人永久挡在门外 |

**权限矩阵变化**（本次新增两行，加进附录 A）：

| 能力码 | researcher | pm | trader | risk | compliance | auditor | admin |
|---|:--:|:--:|:--:|:--:|:--:|:--:|:--:|
| `platform.user.read`（看待审列表） | — | — | — | — | ● | ● | ● |
| `platform.user.manage`（批准/驳回/改档） | — | — | — | — | — | — | ○ |

> `compliance` 能**看**待审列表但不能改 —— 这是刻意的：
> 合规要能核查"有没有不该进来的人"，但**批准权归系统管理员**（避免合规既批又查）。

#### 8.6.3 与套餐层级的关系（§4.6）

注册时用户申请的是**层级**（trial 或 vip），审批时管理员**指定**层级：

```
申请 trial  → 批准 → tenant_id = "trial",  role = researcher, 自选上限 25（5 池 × 5 只）
申请 vip    → 批准 → tenant_id = "vip",    role = researcher, 自选上限 100（5 池）
管理员账号  → 不开放自助注册，只能由现有 admin 创建（tenant_id = "admin"）
```

**升级路径**（试用 → VIP）：只改 `dim_tenant_member.tenant_id`，
**不动 `user_id`** → 他的自选池与个性化参数**天然跟着人走**（§3.3 copy-on-write 的收益）。
这也是 §4.6 反复强调"层级管量、角色管权"的实际好处：**升级不需要迁移任何业务数据。**

#### 8.6.4 认证数据模型

```sql
-- ① 凭证（与 dim_user 一对一，单独一张表：便于单独加密与权限收紧）
CREATE TABLE dim_user_credential (
    user_id        TEXT PRIMARY KEY REFERENCES dim_user(user_id),
    password_hash  TEXT NOT NULL,          -- Argon2id（首选）或 bcrypt(cost≥12)
    password_algo  TEXT NOT NULL DEFAULT 'argon2id',
    password_updated_at TEXT NOT NULL,
    must_change_password INTEGER NOT NULL DEFAULT 0,  -- 管理员重置后强制改
    failed_attempts INTEGER NOT NULL DEFAULT 0,       -- 连续失败计数（本次认证状态）
    locked_until   TEXT,                              -- 临时锁定到什么时候
    last_failed_at TEXT,
    last_failed_ip TEXT,
    -- 历史密码哈希（防"改回旧密码"）：只留最近 5 个
    password_history_json TEXT NOT NULL DEFAULT '[]'
);

-- ② 手机号（**单独一张表 + 可扩展多渠道**）
CREATE TABLE dim_user_contact (
    user_id     TEXT NOT NULL,
    kind        TEXT NOT NULL,             -- phone | email
    value       TEXT NOT NULL,             -- ★ 建议加密存储（至少 phone 要脱敏展示）
    value_hash  TEXT NOT NULL,             -- 盲索引：用于"按手机号查用户"而不解密
    is_primary  INTEGER NOT NULL DEFAULT 1,
    verified_at TEXT,                      -- 验证通过时间（NULL = 未验证）
    created_at  TEXT NOT NULL,
    PRIMARY KEY (user_id, kind, value)
);
CREATE UNIQUE INDEX uq_contact_phone ON dim_user_contact(kind, value_hash)
    WHERE kind='phone';                    -- 一个手机号只能绑一个活跃账号

-- ③ 验证码（短信/邮件共用；**只存哈希**，绝不明文）
CREATE TABLE fact_verification_code (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    scene        TEXT NOT NULL,            -- register | bind_phone | change_phone
                                           -- | reset_password | login_mfa
    target_hash  TEXT NOT NULL,            -- 手机号/邮箱的哈希
    code_hash    TEXT NOT NULL,            -- 验证码的哈希（不存明文！）
    purpose_user TEXT NOT NULL DEFAULT '', -- 变更手机号时：给哪个 user 用
    attempts     INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 5,
    sent_at      TEXT NOT NULL,
    expires_at   TEXT NOT NULL,            -- 建议 5 分钟
    used_at      TEXT,                     -- 用过即作废（一次性）
    request_ip   TEXT,
    send_channel TEXT NOT NULL DEFAULT 'sms',
    provider_msg_id TEXT                   -- 第三方回执 id（排查"用户说没收到"用）
);
CREATE INDEX idx_vcode_lookup ON fact_verification_code(scene, target_hash, expires_at);

-- ④ 记住我（**不是存密码**，是长期 refresh token + 轮换 + 重放检测）
CREATE TABLE fact_remember_token (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    family_id    TEXT NOT NULL,            -- 令牌家族：一次"记住我"登录 = 一个 family
    token_hash   TEXT NOT NULL UNIQUE,     -- 只存哈希
    user_id      TEXT NOT NULL,
    issued_at    TEXT NOT NULL,
    expires_at   TEXT NOT NULL,            -- 🔒 7 天（滑动续期，§8.6.11.1 已确认）
    rotated_from TEXT,                     -- 上一个 token（重放检测用）
    revoked_at   TEXT,
    device_label TEXT NOT NULL DEFAULT '', -- "Chrome / Windows"，让用户能认出设备
    user_agent   TEXT, ip TEXT
);
CREATE INDEX idx_remember_family ON fact_remember_token(family_id);

-- ⑤ 密码重置（一次性高熵令牌 + 必须走验证码）
CREATE TABLE fact_password_reset (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT NOT NULL,
    token_hash  TEXT NOT NULL UNIQUE,      -- 32 字节随机数的哈希
    channel     TEXT NOT NULL,             -- phone | email
    verified_at TEXT NOT NULL,             -- 验证码通过的时间（必须先过验证码）
    expires_at  TEXT NOT NULL,             -- 建议 15 分钟
    used_at     TEXT,                      -- 一次性
    request_ip  TEXT
);
```

#### 8.6.5 六条流程（每条标出**不能做错的那一步**）

**① 注册（含**邮箱**验证 —— 🔒 本期只用邮箱，§8.6.8）**

```
填邮箱 → 发验证码（限流，见 §8.6.7）→ 验码 → 填用户名/密码 → 提交
  → dim_user.status = 'pending'（§8.6.1）
  → 邮箱写入 dim_user_contact 且 verified_at 已填（**注册即验证**）
  → （可选）手机号选填，本期**不验证、不参与找回**
  → 等管理员审批
```

> **不能做错**：**注册阶段就验证邮箱**。理由有三：
> ① 防垃圾注册（§8.6.2 细节 2）；② 为"找回密码"提供已验证通道（**这是本期唯一的找回通道**）；
> ③ **审批人能看到一个已验证的邮箱**，而不是用户随手填的一串字符。

> ⚠️ **"注册即验证邮箱"与"一个邮箱只能有一个账号"要一起定**：
> 建议**允许同一邮箱有多个 pending 申请、但只有一个 active 账号**
> （否则用户注册被驳回后就再也注册不了，得人工清库）。

> 📌 **手机号在本期的定位**：`dim_user_contact` 里的手机号是
> **可选的联系方式**（管理员联系客户用），**不是找回通道、不做验证**。
> 表结构已按"多渠道"设计（`kind = phone | email`），
> 将来接入短信时**不需要改表**，只需把 `verified_at` 填上并打开通道开关（§8.6.8④）。

**② 登录**

```
[登录页] 账号(手机号/用户名) + 密码 + ☑ 记住我
   ↓
① 查账号 → 不存在：返回**统一文案**"账号或密码错误"（不泄露账号是否存在）
② 检查账号状态：pending/rejected → 明确文案；disabled/expired → 明确文案 + 原因
③ 检查本次认证锁定：locked_until > now → 拒绝并告知剩余时间
④ 校验密码（Argon2id）→ 失败：failed_attempts++，达阈值则 locked_until = now + 15min
⑤ 成功：failed_attempts 归零；发 access token（短，15~30min）+ refresh token
⑥ ☑ 记住我 → 额外发 remember token（🔒 **7 天**，§8.6.11.1）
⑦ 写 fact_access_audit（登录成功/失败都要写，含 IP、UA、结果）
```

> **不能做错的三点**：
> ① **密码错误与账号不存在必须返回同一文案**（否则等于免费提供"这个手机号注册了吗"的查询接口）；
> ② **失败计数按账号记，不按 IP 记**（按 IP 会被"换个 IP 继续撞"绕过；按 IP 的部分另有限流层）；
> ③ **登录成功也要审计**——只记失败的话，事后查"谁在什么 IP 登进来的"会没有数据。

**③ 改密码**

```
已登录 → 输旧密码 + 新密码（+ 可选：短信验证码，建议开启）
  → 校验旧密码 → 校验新密码强度（≥10 位、非弱口令、**不在最近 5 个历史密码里**）
  → 更新 password_hash 与 password_history
  → ★ 撤销**其它所有会话**（保留当前这一个）→ 其它设备需重新登录
  → 若来自"记住我"设备：同时撤销该用户的全部 remember token
  → 写审计：action='password_change'
```

> **不能做错**：**改密后必须踢掉其它会话**。用户改密码的第一动机就是"怀疑别人在用我的账号"——
> 如果不踢，那个人的 access token 还有效，改密等于没改。

**④ 记住我（**绝不存密码**）**

```
勾选"记住我" → 服务端发 remember token（随机 32 字节，**只把哈希存库**）
  → 浏览器存 **HttpOnly + Secure + SameSite=Lax** Cookie（**不用 localStorage**）
  → 下次访问：token 换新 access token，**并立即轮换**（旧 token 作废，发新的）
  → ★ 若收到一个**已轮换过的旧 token** → 判定为"令牌被窃取后重放"
     → **立刻撤销整个 family**（该设备的所有令牌）+ 强制重新登录 + 告警
```

> **不能做错的三点**：
> ① **"记住密码"≠ 浏览器存明文密码**。以前端自动填充实现的"记住密码"是
>   把密码交给浏览器密码管理器 —— 那是**用户的选择**，不是**我们存他密码**。
>   服务端**任何情况下都不能存可逆的密码**。
> ② **必须轮换 + 重放检测**（上面那两条）。只发一个长期 token 不轮换 =
>   一旦泄露就是 **7 天的后门**（🔒 窗口口径见 §8.6.11.1），而**用户永远不会知道**。
> ③ Cookie 必须 `HttpOnly`（防 XSS 偷令牌）+ `Secure`（只走 HTTPS）+ `SameSite=Lax`（防 CSRF）。

**⑤ 绑定 / 换绑手机号**

```
首次绑定：已登录 → 输新手机号 → 发验证码 → 验码 → verified_at 落库
换绑：已登录 → ① 验**旧**手机号（证明是本人）
              → ② 验**新**手机号（证明新号可用）
              → ③ 更新 dim_user_contact（旧记录保留为历史，或标记 superseded）
              → ④ 告警到**旧手机号**："你的账号手机号已变更为 138****xxxx，如非本人操作请联系管理员"
```

> **不能做错的两点**：
> ① **换绑必须验旧号**。只验新号的话，攻击者拿到一次会话就能把账号的手机号换成自己的，
>    从此**永久接管**（因为找回密码走手机号）。
> ② **必须通知旧号**。这是用户**唯一可能察觉**账号被盗的时机（他没有被踢下线，
>    但会收到一条短信）。这条告警的价值高于任何技术加固。

**⑥ 找回密码（🔒 本期走**邮箱**；手机号是将来可插拔的通道）**

> **需求演进记录**：原需求写的是"通过手机号**账号密码**找回"；
> 本轮已定"**只用邮箱，短信后置**"（§8.6.8④），所以**本期实现的是邮箱找回**。
> 下面这套流程把"手机号"换成"邮箱"即可，**其余（一次性令牌、限流、撤销会话、告警）完全一致**。

> ⚠️ **先澄清一个歧义**：原需求"通过手机号**账号密码**找回"，
> 如果字面理解成"输手机号 + 密码就能找回"——**那不成立**：
> 知道密码就直接登录了，不需要"找回"。
> 所以正确语义只有两种，本文按 **(b)** 实现：

| 语义 | 实现 | 评价 |
|---|---|---|
| (a) 手机号 + **旧密码** → 重置 | 无意义（知道密码就能登） | ❌ |
| **(b) 手机号 + 短信验证码 → 重置密码** | 标准做法，本文采用 | ✅ |

```
[忘记密码] 输**邮箱**
  ① 无论该邮箱是否存在，**一律返回同一文案**："若该邮箱已注册，验证码已发送"
     （防"邮箱枚举"）
  ② 存在则发验证码（限流）→ 用户输验证码
  ③ 验码通过 → 校验通过次数（≤5）→ 发一次性重置令牌（32 字节随机，存哈希，15 分钟）
  ④ 用户设新密码 → 校验强度 + 不在历史 5 个内
  ⑤ ★ 重置成功 → 撤销该用户**全部**会话 + 全部 remember token
  ⑥ 写审计 + 告警**邮件**："你的密码已重置，如非本人操作请立即联系管理员"

（将来接短信时：只需把"邮箱"换成/加上"手机号"，②的发送通道换掉，
  ①③④⑤⑥ **一行都不用改** —— 这是 §8.6.8④ 保持表结构与流程通用的收益）
```

> **不能做错的三点**：
> ① **不做邮箱枚举**（步骤 ① 的统一文案）；
> ② **重置令牌一次性 + 15 分钟 + 只存哈希**（别学"用 user_id 做参数就能改密"那类实现）；
> ③ **重置后踢掉所有会话** —— 与改密码同理，而且这次更危险：
>    找回密码的常见场景就是"我怀疑被盗了"。
>
> ⚠️ **邮件方案特有的一条**：**验证码邮件必须能真的送达**（SPF/DKIM/DMARC 配好）。
> 短信收不到用户会立刻重试；**邮件进了垃圾箱，用户根本不知道要去哪里找** ——
> 这是从短信换成邮箱后**唯一变差的体验点**，所以：
> ① 注册时**提示"若未收到请检查垃圾邮件"**；
> ② 提供"重新发送"且不消耗额外配额（在限流内）。

#### 8.6.6 认证 API 清单

```
# —— 公开（无需登录，但全部要限流 + 风控）——
POST /api/v1/auth/register              注册（含**邮箱验证码**校验）
POST /api/v1/auth/verify-code           发送验证码 {scene, target, captcha_token}
POST /api/v1/auth/login                 {account, password, remember_me}
POST /api/v1/auth/refresh               用 refresh token 换 access token
POST /api/v1/auth/password/forgot       发起找回 {email}
POST /api/v1/auth/password/reset        用重置令牌设新密码 {token, new_password}
GET  /api/v1/auth/captcha               图形/滑块验证码（发短信前的前置）

# —— 需登录 ——
POST /api/v1/auth/logout                当前会话登出
POST /api/v1/auth/logout-all            撤销该用户全部会话（"我怀疑被盗"按钮）
GET  /api/v1/auth/sessions              列出活跃设备（device_label / ip / 最后活跃）
DELETE /api/v1/auth/sessions/{id}       踢掉某一台设备
POST /api/v1/auth/password/change       {old_password, new_password[, code]}
GET  /api/v1/me/contacts                查看已绑定手机/邮箱（**脱敏展示** 138****1234）
POST /api/v1/me/contacts/phone/bind     首次绑定手机号
POST /api/v1/me/contacts/phone/change   换绑（需旧号 + 新号双验证）
DELETE /api/v1/me/contacts/{kind}/{id}  解绑（**若该号是唯一找回通道，需先绑新号**）
```

> **`/auth/sessions` + `/logout-all` 是必须做的**（不是锦上添花）：
> 用户"怀疑账号被盗"时，唯一能自救的动作就是"看谁登着 + 一键全踢"。
> 没有这两个接口，用户只能来找管理员，而管理员也未必查得清。

#### 8.6.7 六个安全要点（每条对应一个真实的攻击面）

| # | 攻击面 | 处置 |
|---|---|---|
| 1 | **暴力破解 / 撞库** | ① 账号维度失败计数 + 指数退避锁定；② **IP 维度**限流（`/auth/login` 每分钟 N 次）；③ 密码强度（≥10 位 + 弱口令黑名单 + 禁与用户名/手机号相同）；④ **可选 MFA**（TOTP 或短信）——VIP/管理员**建议强制** |
| 2 | **邮箱/手机号枚举** | 注册/找回的响应**一律不区分"该邮箱（手机号）是否已注册"**；验证码接口也要统一文案 + 恒定响应时间（避免时序侧信道） |
| 3 | **短信轰炸（短信炸弹）** | ★ **三重限流**：`(手机号: 1/分钟, 10/日)` + `(IP: 5/小时)` + `(IP 段: 50/小时)`；**发短信前必须先过图形验证码**；验证码**5 分钟过期、一次性、错 5 次作废**；同一手机号两次发送间隔 ≥60 秒 |
| 4 | **令牌/验证码被落库泄露** | 验证码、重置令牌、remember token **一律只存哈希**；`dim_user_credential` 单独成表（便于收紧权限）；手机号加密存储 + 盲索引查询（`value_hash`） |
| 5 | **会话固定 / CSRF / XSS** | 登录成功后**换发新会话 id**；Cookie `HttpOnly + Secure + SameSite=Lax`；前端**不用 localStorage 存令牌**（改 `HttpOnly` Cookie 或内存 + refresh）；所有写接口校验 CSRF token 或 `Origin` |
| 6 | **异常登录不可见** | 新设备/新 IP 登录 → 站内提醒 + （可选）短信；与 §9.1 的 `per_user_qps`、§5.3 审计联动；**登录失败率突增**要能告警（撞库的典型信号） |

#### 8.6.8 短信通道：**国内短信没有"免费"这条路**（已核实现行政策）

> **需求问**："短信通道有哪些免费的工具吗？"
> **答案：对国内手机号，没有。** 而且原因的根子不在价格，在**准入**。

**① 硬约束（阿里云官方文档，2026 现行）**

| 约束 | 原文口径 | 对你的影响 |
|---|---|---|
| **只有企业资质能报备发送** | "应[签名实名制报备](https://help.aliyun.com/zh/sms/user-guide/real-name-reporting-of-sms-sign-name)要求，**仅企业资质才可以进行报备和发送短信**"（[使用须知](https://help.aliyun.com/zh/sms/user-guide/usage-notes)） | ⚠️ **个人账号自用资质走不通** |
| 个人认证只能用"他用资质" | 需提供**企业**证照 + **委托授权书**（[资质材料说明](https://help.aliyun.com/zh/sms/user-guide/qualification-application-description)） | 得找一家公司给你授权 |
| **签名实名报备要 5~10 个工作日** | "运营商实名报备流程平均需要 5-7 个工作日…**部分运营商需要 7-10 个工作日**，但运营商未对此时效进行承诺，**实际可能需要更长时间**" | 🔴 **这是上线阻塞项**：开发 1 天，报备 2 周 |
| 签到/模板审核 | 资质约 2 个工作日；签名与模板约 2 小时 | 相对快 |
| 个人认证**不支持**推广短信、多媒体短信、国际/港澳台短信 | 同上文档的权益对比表 | 我们只用"验证码"类，**不受影响** |
| 官方对个人的建议 | "无法提供企业资质信息的个人认证用户**推荐使用短信认证产品**" | 那是阿里云的另一款托管产品，按次计费，**不是免费** |

**② 更隐蔽的一条：能用 ≠ 免费**

即使拿到企业授权，国内短信也是**按条计费**（约 ¥0.03~0.05/条，量大递减）。
**所以"免费短信"这条路在架构上就不该被依赖** —— 依赖它意味着
"通道一停，用户就登不进系统"。

**③ 三条可落地的替代路线（按推荐度排）**

| 路线 | 成本 | 准入 | 适合度 |
|---|---|---|---|
| ★★★ **邮箱验证码**（走 SMTP / 事务邮件） | **真免费**（自有邮箱/企业邮；第三方事务邮件有免费额度） | **无准入**，不需报备 | **最推荐**。密码找回、注册验证都能覆盖 |
| ★★ **邮箱优先 + 短信可选**（混合） | 短信按条计费 | 短信侧仍需企业资质 | **本文推荐方案**（见下 ④） |
| ★ **短信认证类托管产品**（阿里云"短信认证服务"等） | 按次计费 | 门槛低于自建签名 | 想要短信体验但拿不到企业资质时的折中 |
| — | 第三方"免费短信 API" | —— | ❌ **不要用**：来源不明的通道常伴随号码泄露与合规风险 |
| — | 自建短信猫 / 卡池 | 设备+卡费 | ❌ 个人自用尚可，**对外服务不合法也不稳定** |

**④ 🔒 本轮决策（2026-09）：只用邮箱，短信后置**

> **需求方决定**："使用邮箱吧，短信能力后面再说。"
> 这是**对的取舍** —— 它把"上线阻塞 5~10 个工作日的报备"从关键路径上移走了。

```
本期（只做邮箱）：
  注册验证 / 找回密码 / 换绑  → **全部走邮箱**（SMTP，免费、无准入）
  手机号                      → 选填，仅作联系方式，**不验证、不参与找回**
  通道裁决                    → 简化为单通道，不需要"按用户挑通道"的逻辑

将来（接短信时，不改表、不改流程）：
  ① 打开通道开关并配置签名/模板（报备完成后再做）
  ② 把手机号的 verified_at 填上（走一次 `bind_phone` 验证）
  ③ 通道裁决恢复为：有已验证手机号且短信通道可用 → 短信，否则邮箱
  → `dim_user_contact` 已按多渠道设计（kind = phone | email），**无需迁移**
```

> ⚠️ **两个仍然必须保留的纪律**（与"用哪个通道"无关）：
> 1. **不允许存在"无任何已验证找回通道"的账号** —— 本期就是**邮箱必须已验证**。
>    这种账号一旦忘密码只能找管理员手工重置（把运维成本转嫁出去）。
> 2. **额度耗尽/发送失败必须"明确拒绝"**，不能静默失败（否则用户一直等邮件）。
>    本期配额的 `channel` 就只有 `email` 一行，但表结构与判定逻辑**照建** ——
>    将来加 `sms` 只是多一行，不用改代码。


**⑤ 成本与配额（回应"短信费用计入租户配额"）**

```sql
-- ① 套餐配额里加通道额度
ALTER TABLE dim_tier ADD COLUMN monthly_sms     INTEGER NOT NULL DEFAULT 0;   -- 每月短信条数上限
ALTER TABLE dim_tier ADD COLUMN monthly_email   INTEGER NOT NULL DEFAULT 0;   -- 每月邮件封数上限

UPDATE dim_tier SET monthly_sms=0,    monthly_email=2000 WHERE tier_code='trial';
UPDATE dim_tier SET monthly_sms=200,  monthly_email=5000 WHERE tier_code='vip';
UPDATE dim_tier SET monthly_sms=1000, monthly_email=9999 WHERE tier_code='admin';

-- ② 按 (租户, 渠道, 计费月) 累计用量 —— 用于"配额判定"与"成本对账"
CREATE TABLE fact_notify_usage (
    tenant_id   TEXT NOT NULL,
    channel     TEXT NOT NULL,            -- sms | email
    period      TEXT NOT NULL,            -- 'YYYY-MM'（按自然月归零）
    sent_count  INTEGER NOT NULL DEFAULT 0,
    failed_count INTEGER NOT NULL DEFAULT 0,
    cost_yuan   REAL NOT NULL DEFAULT 0,  -- ★ 按条单价累计，**真金白银要能对账**
    updated_at  TEXT NOT NULL,
    PRIMARY KEY (tenant_id, channel, period)
);

-- ③ 每条外发记录（排查 + 审计 + 幂等）
CREATE TABLE fact_notify_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL,
    tenant_id   TEXT NOT NULL,
    user_id     TEXT NOT NULL DEFAULT '',
    session_id  TEXT NOT NULL DEFAULT '',   -- ★ 会话级隔离（§8.6.11）
    channel     TEXT NOT NULL,              -- sms | email
    target_hash TEXT NOT NULL,              -- 手机号/邮箱的哈希（**不存明文**）
    scene       TEXT NOT NULL,              -- register | reset_password | change_phone | login_alert
    provider    TEXT NOT NULL DEFAULT '',   -- aliyun | tencent | smtp | console
    provider_msg_id TEXT,
    unit_price  REAL NOT NULL DEFAULT 0,    -- 当时的单价（改价后历史可复算）
    status      TEXT NOT NULL,              -- sent | failed | rate_limited | quota_exceeded
    error_code  TEXT,
    error_msg   TEXT NOT NULL DEFAULT ''
);
CREATE INDEX idx_notify_usage ON fact_notify_log(tenant_id, channel, at);
```

**⑥ 通道抽象（关键：**渠道是可切换的策略，不是硬编码**）**

```python
# src/infrastructure/notify/  （新增；注意不是 sms/ —— 因为它同时管邮件）
class Notifier(Protocol):
    channel: str                                  # 'sms' | 'email'
    async def send(self, target: str, template: str, params: dict) -> str:
        """返回 provider_msg_id（用于排查"用户说没收到"）。"""

class SmtpEmailNotifier:   ...   # ★ 免费主力：走 SMTP（自有邮箱/企业邮）
class AliyunSmsNotifier / TencentSmsNotifier / YunpianSmsNotifier: ...  # 付费可选
class ConsoleNotifier: ...       # dev/test：把验证码打到日志（**仅非 prod 允许**）
class FakeNotifier:    ...       # 单测：验证码写进内存供断言

# 发送时的通道裁决（§8.6.8④ 的落点）
async def send_code(user_id: str, scene: str) -> str:
    if await has_phone(user_id) and sms_configured():
        return await notifier("sms").send(...)
    if await has_email(user_id):
        return await notifier("email").send(...)
    raise NoRecoveryChannel("请补充邮箱或联系管理员")   # ★ 见上方纪律
```

**四条纪律**：

| # | 纪律 | 原因 |
|---|---|---|
| 1 | **只允许非 prod 使用 `ConsoleNotifier`** | 生产打日志 = 验证码泄露。用 §8.7.2 的启动自检拦住（`env=prod` 且通道是 Console → **拒绝启动**） |
| 2 | **额度耗尽要走"明确拒绝"而不是"静默失败"** | 返回 `status='quota_exceeded'` + 前端提示"本月验证码额度已用完，请联系管理员"；**不能假装发送成功**（用户会一直等短信） |
| 3 | **邮件必须配好 SPF/DKIM/DMARC** | 否则验证码进垃圾箱 = 用户收不到 = 以为系统坏了。**这是邮件方案唯一的坑** |
| 4 | **单价落库（`unit_price`）** | 通道会调价；不存历史单价，事后的成本对账就永远对不上 |

#### 8.6.9 前端要点（体验与安全的交界）

| 项 | 做法 |
|---|---|
| "记住我" | 勾选框默认**不勾**；文案写清"保持登录 30 天，公共电脑请勿勾选" |
| 密码框 | `type=password` + `autocomplete="current-password"`/`"new-password"`（让浏览器密码管理器正确工作）；提供"显示密码"开关 |
| 强度提示 | 实时提示（长度/字符类型/是否弱口令），**不阻塞输入**，只在提交时校验 |
| 验证码倒计时 | 60 秒倒计时；倒计时中禁用按钮但**显示剩余秒数**（不显示秒数用户会连点） |
| 找回密码入口 | 登录页显著位置；**不要求先登录**（否则忘密码的人进不去） |
| 错误文案 | 统一、不含内部信息（不复述 SQL/异常栈）；但**账号状态类错误要具体**（"账号待审批"/"账号已到期"）——那对用户是真信息，不是泄露 |
| 会话管理页 | 列设备（`device_label` + 最后活跃 + IP 归属地可选）；"退出所有设备"按钮**放在显眼处** |

#### 8.6.10 管理员控制台（租户 / 用户管理界面）

> **需求原文**："需要做个管理员管理租户的界面，可以参考业界。"
> 本节给**信息架构 + 交互规则 + API 清单 + 反向规则**。
> 参考对象：AWS IAM 用户列表、Stripe 的订阅/客户管理、QuantConnect 的 Organization Members、
> 以及本项目已有的 `AdminUserTable`（`quant/quantCode/QuantMind/electron/src/features/admin/`）。

##### 8.6.10.1 五个屏（信息架构）

```
① 概览 Dashboard
   ├ 待办卡片：待审注册 N / 7 天内到期 M / 已到期未处理 K / 配额告警 J
   ├ 规模卡片：各层级人数、活跃数（近 7 日有登录）、平均自选数
   └ 平台资源卡片：宇宙标的数 / 数据源健康 / 重任务队列深度（§9.1 指标）

② 用户列表（主界面）
   筛选：层级(tier) · 状态 · 到期区间 · 最后登录区间 · 自选数区间 · 关键词(用户名/邮箱/ID)
   排序：到期时间（默认升序，最紧急在最上）· 注册时间 · 最后登录 · 自选数
   列： 用户名 | 层级 | 角色 | 状态 | 自选 x/上限 | 最后登录 | **有效期至**（带色阶） | 操作

③ 审批工作台（pending 队列）
   ├ 左：待审列表（按申请时间排序）  右：详情（申请层级 / 理由 / 来源 IP / 重复注册检测）
   └ 批量：批准 N 个 / 驳回 N 个（驳回必须填原因）

④ 用户详情抽屉（点行展开，不跳页）
   标签页：概览 · 配额与有效期 · 权限(能力码增量) · 通知设置 · **操作流水**（fact_registration_review）

⑤ 层级与配额配置（dim_tier 的 CRUD）
   └ 改这里影响所有人 → 四眼 + 影响预览（"当前会影响 300 个 VIP 用户"）
```

##### 8.6.10.2 列表行的三个关键可视（业界通行且有效）

| 元素 | 做法 | 为什么 |
|---|---|---|
| **用量进度** | `自选 87/100` 用进度条而非纯文字 | 管理员一眼看出谁快满了（要不要提醒升级） |
| **有效期色阶** | 剩余 >30 天 灰 / 7~30 天 黄 / <7 天 红 / 已过期 深红+删除线 | 到期管理是这界面的**核心任务**，必须一眼可扫 |
| **状态徽标** | `active` 绿 / `pending` 蓝 / `expired` 橙 / `disabled` 灰 / `deleted` 暗灰 | 与 §8.6.1 的七态一一对应，**颜色不重复** |

##### 8.6.10.3 六条交互规则（每条都防一类事故）

| # | 规则 | 防的事故 |
|---|---|---|
| 1 | **管理员不能修改自己**的层级/配额/有效期（除改密码）；改自己要第二人审批 | 自我提权 / 自我续期 —— 这是内部平台最常见的越权路径 |
| 2 | **危险操作二次确认必须"打字确认"**：软删除/匿名化要求输入目标用户名（不是"确定/取消"按钮） | 列表里点错行 = 删错人。打字确认是业界（GitHub/AWS）对不可逆动作的标准做法 |
| 3 | **操作前显示影响预览**："将禁用 u-123（VIP，自选 87 只），其 1 个 WS 连接将在宽限后断开" | 管理员往往不知道"禁用"对正在盘中的人意味着什么 |
| 4 | **批量操作上限 + 事后报告**：单次批量 ≤50，执行完给出逐条结果（成功/失败原因） | 批量删 300 人却不知道哪些失败 = 事故 |
| 5 | **列表默认排除 `deleted`**，需显式勾选"包含已删除"才显示 | 否则已删除用户永远混在列表里 |
| 6 | **所有写操作走同一套 API**（不允许前端直连 DB/脚本改库），且都落 `fact_registration_review` | 有旁路 = 审计不完整 |

##### 8.6.10.4 API 清单（全部需要 `platform.user.manage`，写操作需四眼）

```
GET    /api/v1/admin/overview                    概览卡片数据
GET    /api/v1/admin/users                       列表（筛选/排序/分页）
GET    /api/v1/admin/users/{id}                  详情（含流水）
POST   /api/v1/admin/users                       新建（管理员建号，不进 pending）
PATCH  /api/v1/admin/users/{id}                  改资料（display_name/email/note）
POST   /api/v1/admin/users/{id}/approve          批准（可指定 tier + valid_until）
POST   /api/v1/admin/users/{id}/reject           驳回（note 必填）
POST   /api/v1/admin/users/{id}/disable          禁用
POST   /api/v1/admin/users/{id}/enable           启用
POST   /api/v1/admin/users/{id}/renew            续期（valid_until 必填，向前顺延）
POST   /api/v1/admin/users/{id}/tier             改层级（tier_code）
POST   /api/v1/admin/users/{id}/roles            改角色（roles 列表）
POST   /api/v1/admin/users/{id}/quota            改个人配额覆盖（可选，见下）
DELETE /api/v1/admin/users/{id}                  软删除（?mode=soft|anonymize）
POST   /api/v1/admin/users/{id}/restore          恢复（soft 可逆；anonymize 不可）
POST   /api/v1/admin/users/batch                 批量（action + ids[]，≤50）
GET    /api/v1/admin/tiers                       层级配置
PATCH  /api/v1/admin/tiers/{code}                改层级（四眼 + 影响预览）
GET    /api/v1/admin/reviews                     操作流水（可按 user/action/时间筛）
```

##### 8.6.10.5 个人配额覆盖（`dim_user_quota_override`）

真实运营里总会遇到"这个 VIP 客户特殊，给他 150 只"：

```sql
CREATE TABLE dim_user_quota_override (
    tenant_id  TEXT NOT NULL,
    user_id    TEXT NOT NULL,
    key        TEXT NOT NULL,     -- watchlist_limit / pool_limit / valid_until / daily_llm_tokens ...
    value      TEXT NOT NULL,
    reason     TEXT NOT NULL,     -- 必填：为什么要给他破例
    granted_by TEXT NOT NULL,
    expires_at TEXT,              -- 【强制】破例也要有期限
    PRIMARY KEY (tenant_id, user_id, key)
);
```

**取生效配额的顺序**（与 §3.3 copy-on-write 同一思路）：

```
生效配额 = 个人覆盖（未过期）  >  套餐层级默认（dim_tier）
```

> ⚠️ **两条约束**：① 个人覆盖**只能放宽到管理员批准的上限**，不能突破平台硬上限；
> ② **覆盖必须有 `expires_at`** —— 否则"临时破例"会变成"永久特权"，
> 而没人记得当初为什么给他。这与 §4.3.1 的 break-glass 强制到期是同一条原则。

##### 8.6.10.6 管理员的"增加用户"与自助注册的关系

| 入口 | 状态 | 用途 |
|---|---|---|
| 自助注册 | 落 `pending`，等审批 | 对外开放（试用申请） |
| **管理员新建** | 直接 `active`（仍需填 `valid_until`） | 内部开号 / 代客户开号 |
| 批量导入（CSV） | 落 `active`，逐行校验；失败行报告 | 客户批量开通 —— **每行都要有 `valid_until`** |

> **为什么管理员新建可以跳过审批**：审批的目的是"**确认这个人该不该进来**"，
> 而管理员建号这个动作**本身就是那个确认**。但**留痕不能少**：
> `fact_registration_review` 记 `action='approve'` + `reviewer_id=管理员` + `note='管理员新建'`。

#### 8.6.11 会话认证与 sessionId 级隔离

> **需求原文**："要支持 sessionid 级别的信息隔离，支持登录短期的 cookie 认证，
> 用户短期不看，同一设备再次打开网页时不用再输密码认证。"
>
> 这条需求里藏着**一对相反的边界**，必须一起设计，否则会做成两种坏结果之一：
> ① 边界太长 → 会话永不失效（笔记本丢了 = 账号丢了）；
> ② 边界太短 → 用户每次回来都被要求输密码（"不是说了不用再输吗"）。

##### 8.6.11.1 两层凭证：**session cookie 管"这一会儿"，refresh cookie 管"这台设备"**

```
【第一层】session cookie（短期，默认 30 分钟滑动）
   目的：日常鉴权凭证 + **会话级隔离的锚点**
   行为：每次有请求就续期（滑动窗口），但**有绝对上限**（默认 12 小时）

【第二层】refresh cookie（长期，默认 7 天滑动，上限 30 天）
   目的：**免密回到页面**（"短期不看再打开不用输密码"）
   行为：用它静默换一个新的 session cookie；**换新即轮换**（§8.6.5④ 的重放检测）

【用户视角的时间线】
  离开 10 分钟  → session 还在 → 直接进（无感）
  离开 2 小时   → session 过期、refresh 有效 → 静默换新 → 直接进（无感）★ 需求要的
  离开 8 天     → refresh 过期 → 要求输密码（这是应该的）
```

> **"短期不看"的口径 —— 🔒 已确认（2026-09）：7 天免密窗口**
>
> 需求方原话确认："**一天内再打开 → 7 天（本文默认）**"。
> 即：**只要间隔不超过 7 天，同一设备再打开网页都不用输密码**；
> 超过 7 天才要求重新认证。三档对照（供日后调整时参考）：
>
> | 解释 | refresh 窗口 | 安全含义 | 结论 |
> |---|---:|---|---|
> | "几分钟到几小时" | 1~2 天 | 最保守 | 太紧，"一天内再打开"就输了 |
> | **"一天内再打开"** | **7 天** | 平衡点 | ★ **采用** |
> | "一两周才看一次" | 30 天 | 笔记本丢了 = 一周后仍可被进 | 不采用 |
>
> ⚠️ **记住这条的代价**：**"免密窗口"就是"设备被物理接触后可被访问的窗口"**。
> 7 天意味着——笔记本丢了、或在网吧忘了退，7 天内捡到的人可以免密进入。
> 所以配套必须做两件事（都已落在 §8.6.6 / §8.6.11.5）：
> ① `/auth/sessions` 让用户**能看见并按设备踢**；
> ② 改密码/重置密码时**撤销全部 refresh token**（否则"改密"对已记住的设备无效）。

```sql
-- 会话（**sessionId 级隔离的载体**）
CREATE TABLE fact_session (
    session_id     TEXT PRIMARY KEY,        -- 服务端随机 32 字节（hex），**客户端不可伪造**
    user_id        TEXT NOT NULL,
    tenant_id      TEXT NOT NULL,
    console        TEXT NOT NULL DEFAULT 'default',   -- research | invest（§9.4）
    -- 凭证
    access_jti     TEXT NOT NULL UNIQUE,    -- 当前 access token 的 id（用于精确吊销）
    refresh_hash   TEXT,                    -- refresh token 哈希（不透出）
    -- 生命周期
    created_at     TEXT NOT NULL,
    last_seen_at   TEXT NOT NULL,           -- ★ 每次请求更新（滑动窗口的依据）
    idle_expires_at TEXT NOT NULL,          -- 滑动过期点（= last_seen + idle_ttl）
    absolute_expires_at TEXT NOT NULL,      -- ★ 绝对上限（不可被滑动突破）
    revoked_at     TEXT,
    revoke_reason  TEXT,                    -- logout | logout_all | password_change |
                                            -- password_reset | admin_kick | replay_detected
    -- 设备与环境（让用户认得出、让风控判得出）
    device_label   TEXT NOT NULL DEFAULT '',-- "Chrome / Windows"
    device_id      TEXT NOT NULL DEFAULT '',-- 前端生成的稳定设备指纹（非指纹追踪，仅本机绑定）
    user_agent     TEXT, ip TEXT, cf_country TEXT
);
CREATE INDEX idx_sess_user   ON fact_session(user_id, revoked_at);
CREATE INDEX idx_sess_expire ON fact_session(idle_expires_at, absolute_expires_at);
```

##### 8.6.11.2 Cookie 三条（哪条设错都会出问题）

| Cookie | 内容 | 属性 | 说明 |
|---|---|---|---|
| `moss_sid` | **session_id** | `HttpOnly; Secure; SameSite=Lax; Path=/` **无 Max-Age（会话级）** | 关浏览器即丢 —— 这是**"短期"的第一层**。**不是 JWT**（见下） |
| `moss_rt` | refresh token | 同上 + `Max-Age=7d` + **`Path=/api/v1/auth/refresh`**（★ 只在换新时发送） | 收窄 Path 可显著减小暴露面 |
| `moss_csrf` | CSRF token | **非 HttpOnly**（前端要读）+ `SameSite=Lax` | 双提交校验用 |

> ⚠️ **为什么 session cookie 里放的必须是"不透明 ID"而不是 JWT**：
> JWT 无法即时吊销（只能等过期）。而这条需求要的是**会话级隔离**——
> 那就必须能**立刻踢掉某一个 session**（用户点了"退出这台设备"、或管理员"踢人"、
> 或检测到重放）。**不透明 ID + 服务端 `fact_session` 查表**才能做到。
> access token 仍可用 JWT（短期，15~30 分钟），但**会话的锚点是 `session_id`**。

##### 8.6.11.3 "短时间不看"到底走哪条路（三种情况，别混）

| 用户行为 | session cookie | refresh cookie | 结果 |
|---|---|---|---|
| 刷新/切标签页（< 30 分钟） | ✅ 仍在（滑动续期） | 不用动 | **无感** |
| 关浏览器 2 小时后重开 | ❌ 已丢（会话级 Cookie） | ✅ 有效 → **静默换新 session** | **无感** ← 需求要的这个 |
| 关浏览器 8 天后重开 | ❌ | ❌ 过期 | 要求输密码（**应该的**） |

> **滑动的正确实现（容易做反）**：
> ```
> 每次**有真实业务请求** → last_seen_at = now; idle_expires_at = now + 30min
> 绝对上限 absolute_expires_at **永不延长** → 到点无论多活跃都失效
> ```
> ① "真实业务请求"**不含**心跳/WS ping —— 否则一个后台标签页能把会话续成永久；
> ② **绝对上限不能省** —— 没有它，一个被窃的 cookie 只要持续发请求就**永不失效**；
> ③ WS 连接**不续期**（它只是长连接，不代表人在用）。

##### 8.6.11.4 sessionId 级隔离：**隔离什么、不隔离什么**

> ⚠️ **先纠正一个容易做错的直觉**：不是"所有数据都按 session 隔离"。
> 行情是公共的（§3.2 铁律），按 session 隔离会让缓存命中率归零。
> **sessionId 隔离的是"会话态"，不是"用户数据"、更不是"市场数据"。**

| 类别 | 归属键 | 例子 |
|---|---|---|
| **市场数据（L0）** | **不隔离**（全局共享） | 行情、因子、板块、缠论 |
| **用户数据（L2）** | `(tenant_id, user_id)` —— **跨会话共享** | 自选池、个股参数档案、提醒设置 |
| **会话态（新）** | **`session_id`** —— 会话间**互相不可见** | 当前查看的池 / 当前标的、草稿（未保存的权重调整）、筛选与排序、分页位置、看板布局、WS 订阅集合、会话级限流计数 |
| **安全态** | `session_id` | 登录时间/设备/IP、CSRF token、MFA 通过状态 |

**落点（三处必须改）**：

```python
# ① 请求上下文：Principal 增加 session_id
@dataclass(frozen=True)
class Principal:
    user_id: str
    tenant_id: str
    session_id: str = ""        # ★ 新增：会话级隔离的锚点
    ...

# ② 会话态缓存键：必须含 session_id
def session_cache_key(parts: str) -> str:
    p = require_principal()
    return hashlib.sha256(
        f"{p.tenant_id}|{p.user_id}|{p.session_id}|{parts}".encode()
    ).hexdigest()
# 用法：current_pool / current_symbol / draft_weights / filters ...

# ③ WS 连接注册表：按 session 索引（不是按连接）
ws_registry: dict[str, set[WebSocket]] = {}   # session_id -> sockets
# 用途：① 同一 session 多标签页共享订阅（不重复算）；
#       ② 该 session 被踢 → 立刻关掉它的**全部** socket，不留悬挂连接
```

**`_watch_cache` / 口径指纹的关系（要说清，否则会过度隔离）**：

| 层 | 缓存键 | 是否含 session |
|---|---|---|
| 行情/因子（L0） | `(code, trade_date)` | ❌ 不含（公共） |
| 打分结果 | `(code, 口径指纹)` | ❌ 不含（口径相同即共享，即使来自不同用户） |
| **会话态** | `(tenant, user, session, parts)` | ✅ 含 |

> **判断标准一句话**：**"这份数据在另一个会话里看到，用户会不会觉得不对？"**
> 会 → 按 session 隔离（草稿、当前池）；不会 → 不隔离（行情、他自己的自选池）。

##### 8.6.11.5 两个必须处理好的边界情况

| 情况 | 正确处理 | 错误做法的后果 |
|---|---|---|
| **用户开了 3 个标签页** | 同一 `session_id`（共享 Cookie）→ **共享订阅**，只算一次 | 按标签页各自建 session → 3 倍取数与推送 |
| **用户在两台设备登录** | 两个 session，互不影响；`/auth/sessions` 都能看到，可单独踢 | 后一次登录踢掉前一次 → "我在手机上登录，电脑就被踢了"（体验事故） |

**安全联动（与 §8.6.5 呼应）**：

| 事件 | 对 session 的动作 | 对 WS 的动作 |
|---|---|---|
| 改密码 / 重置密码 | **撤销该用户全部 session**（§8.6.5③⑥） | 全部关闭 |
| 检测到 remember token 重放 | 撤销**整个 family** 对应的 session + 告警（§8.6.5④） | 全部关闭 |
| 管理员"踢人" | 按 `session_id` 精确撤销（§8.6.10.4 的 kick 接口） | 该 session 的连接关闭 |
| 账号 `disabled` | 撤销全部 session | **立刻关闭**（无宽限） |
| 账号 `expired`（到期） | 撤销全部 session | **宽限期内先"只读降级"，宽限结束再关**（§8.6.1.2） |
| 会话到期（`idle`/`absolute`） | 该 session 失效 | 关闭，前端**静默 refresh 后重连** |

##### 8.6.11.6 三条"会话/账号失效"路径必须分开处理（**需求方特别强调**）

> **需求原文（本轮）**："**会话到期**时前端必须'静默 refresh'；
> **账号到期或被 disabled** 时要**立刻关 WS**。"

这条要求本身是对的，但**三种失效的处置并不相同**——如果实现成一套逻辑，会出现两个坏结果：
① 会话一到期就把用户踢回登录页（他每 30 分钟被打断一次）；
② 账号一到期就立刻切断（正在盘中的人被踢，而他的到期只是"该续费了"）。

**三路径对照（这张表是本节的核心）**：

| | ① **会话到期**（要续，别打扰） | ② **账号 disabled**（要断，且立刻） | ③ **账号 expired**（要断，但给宽限） |
|---|---|---|---|
| **性质** | 技术状态 | **管理员的强制措施** | 业务状态（欠费/到期） |
| **谁触发** | 时间（30 分钟无操作 / 12 小时绝对上限） | 管理员动作，或风控判定 | 时间（`valid_until` 到点） |
| **能自动恢复吗** | ✅ 有 refresh 就自动续 | ❌ 必须管理员解封 | ✅ 续期即恢复（§8.6.1.2） |
| **HTTP 接口** | 返回 401 + `{"code":"session_expired"}` | 返回 403 + `{"code":"account_disabled", "reason":...}` | 返回 403 + `{"code":"account_expired", "days":...}` |
| **前端反应** | **静默 refresh** → 成功则重放原请求（用户**完全无感**）；失败才跳登录页 | **直接跳登录页** + 显示原因（refresh 一定会失败，不必浪费一次往返） | 跳"续费/联系管理员"页（**不是**登录页——他不是认证问题） |
| **WS 处置** | 关闭 → **前端静默 refresh 后自动重连**（不提示） | **立刻关闭**（无宽限） | **宽限期内只读降级，宽限结束关闭** |

**WS 这一侧的四个实现要点（漏一个就出问题）**：

```
① WS 无法像 HTTP 那样"返回 401 让前端自己处理" ——
   所以服务端必须**主动推送一条控制帧再关闭**：
     {"type":"auth_expired","reason":"session_expired|account_disabled|account_expired"}
   前端据此决定"静默重连"还是"跳登录页"。**没有这条帧，前端只能猜。**

② close code 要能区分原因（自定义 4000 段，别都用 1000）：
     4001 session_expired      → 前端：refresh 后重连
     4002 account_disabled     → 前端：跳登录页 + 提示原因
     4003 account_expired      → 前端：跳续费页
     4004 session_revoked      → 前端：跳登录页（被踢）
   用 1000（正常关闭）会让前端以为"网络断了"→ 无限重连（§8.5.2 第 1 条的退避也因此失效）。

③ ★ **关闭动作必须在服务端做，不能只靠客户端自觉**
   服务端一撤销 session，推送循环就**再也取不到该 session 的数据**（订阅表按 session_id 索引，
   §8.6.11.4）。这样即使客户端故意忽略那条关闭帧，**它也不会再收到任何新数据**。
   只发帧不切断 = "通知一个恶意客户端停止收数据"，那是无效的。

④ **多副本时"立刻"要跨进程**（§8.5.2 第 3 条同源）：
   管理员在 api#2 禁用账号，而用户的 WS 挂在 realtime 进程上 →
   撤销事件必须走 **Redis 发布订阅广播**，realtime 收到后关连接。
   **只更新数据库是"慢慢生效"**（等下一次鉴权才发现），不是"立刻"。
```

**"账号到期"为什么要宽限、而"disabled"不要**（这是两条需求的分界）：

| | 理由 |
|---|---|
| `expired` 给宽限 | 到点是**时间**造成的，不是用户做错事。用户正在盘中盯着一只票，因为 10:00 到期就被切断 = **体验事故**。所以：宽限期内**保持连接但降级**——停止该用户的新订阅、面板标"账号已到期（只读）"、**不再推送信号/提醒**（不给已过期账号持续提供付费价值），宽限（默认到当个交易时段结束或 30 分钟）后关闭 |
| `disabled` 不给宽限 | 它是**管理员的强制措施**（封号）。此时每一秒的连接都是"在被封账号上继续提供数据"，**必须立刻断**。任何宽限都等于"封号没生效" |

> **一句话**：**"到期"是欠费，"禁用"是封号。**
> 欠费给几分钟收拾东西，封号当场断线 —— 这是所有 SaaS 的通行做法，
> 也与 §8.6.1.2 的"不能到点硬切断"一致（那条讲的正是 `expired`，不是 `disabled`）。

**前端实现的三个细节**：

| 细节 | 做法 |
|---|---|
| 静默 refresh 的**并发去重** | 一屏可能同时发 5 个请求，5 个都收到 401 → 必须**只发一次 refresh**（共享同一个 Promise），其余等结果。否则会打 5 次 refresh，而**带轮换的 refresh 第 2 次就会判定重放**（§8.6.5④）→ **把用户自己的会话全撤销**，这是最隐蔽的一个坑 |
| refresh 成功后要**重放原请求** | 不是让前端重新点一次；把失败的请求排队，拿到新 access token 后自动重放 |
| 重连 WS 的退避 | 静默重连也要**指数退避 + 抖动**（§8.5.2 第 1 条），否则 100 个客户端同一秒重连会打出一个握手洪峰 |


### 8.7 开发 / 测试 / 运维 三环境分离（合规强制）

> **依据**：《证券基金经营机构信息技术管理办法》**第二十一条**
> "须有独立于生产环境的开发测试环境；测试环境使用未脱敏数据须采取与生产环境**同等**的安全控制"；
> **第三十二条** "对重要信息系统实施**开发、测试、运维分离**，保证岗位相互制衡"。

#### 8.7.1 三环境的差异（不是"换个库名"那么简单）

| 维度 | dev（开发） | test/staging（测试） | prod（生产） |
|---|---|---|---|
| 数据 | 合成数据 / 冻结录像带 | **脱敏副本**（或同等安全控制） | 真实数据 |
| 数据库 | SQLite 文件 | 独立 Postgres 实例 + 独立 schema | 生产 Postgres（RLS 强制） |
| 数据源 | 允许 `simulated` 兜底**仅限 dev** | 指向数据源的**录制回放**（不改真实状态） | 真实源，**绝不回退 simulated** |
| LLM | 可指向本地小模型 | 可指向本地模型 | 真实路由 + 预算 |
| 鉴权 | `MOSS_ALLOW_HEADER_IDENTITY=1` 可开 | 强制鉴权，测试账号 | 强制鉴权 + 真实 IdP |
| 定时任务 | 全关 | 只跑手动触发 | 全开 |
| 权限 | 开发角色 | 测试角色（**不得有生产数据权限**） | 生产角色 |

> ⚠️ 现有代码里 **`configs/intraday.yaml` 是"前端可写"**（加自选会重写 YAML）——
> 在 dev 环境这很方便，但**生产环境不该让业务操作改配置文件**（§6.1 已改为落库）。
> 这正是三环境分离的意义：**同一个功能在 dev 用文件、在 prod 用数据库**。

#### 8.7.2 防"误连生产"的启动自检（**这一条最值钱**）

三环境最大的风险不是"配错"，而是"**以为连的是测试，实际连的是生产**"。所以：

```python
# src/core/config.py 建议新增（伪码）
class Settings(BaseSettings):
    env: Literal["dev", "test", "prod"] = "dev"     # 必须显式声明

def assert_environment_consistency(s: Settings) -> None:
    """启动自检：环境标记与连接目标必须自洽，否则**拒绝启动**。"""
    if s.env == "prod":
        problems = []
        if s.data_backend != "postgres":
            problems.append("生产必须用 postgres（SQLite 单写者无法多副本）")
        if "localhost" in s.postgres_dsn or "127.0.0.1" in s.postgres_dsn:
            problems.append("生产 DB 不应是 localhost")
        if os.environ.get("MOSS_ALLOW_HEADER_IDENTITY"):
            problems.append("生产禁止开启请求头身份（可伪造越权）")
        if not os.environ.get("MOSS_TENANCY_ENFORCE"):
            problems.append("生产必须强制鉴权")
        if not os.environ.get("MOSS_IDENTITY_SECRET"):
            problems.append("生产必须配置身份签名密钥")
        if problems:
            raise ConfigError("生产环境配置不自洽，拒绝启动：\n  - " + "\n  - ".join(problems))
    if s.env != "prod" and s.postgres_dsn and "prod" in s.postgres_dsn.lower():
        raise ConfigError("非生产环境指向了疑似生产库，拒绝启动（防误操作）")
```

**为什么"拒绝启动"而不是"打警告"**：警告会被日志淹没。
**启动失败是唯一 100% 会被看见的提示**——这与项目既有的
"让漏配表现为失败，而不是静默无隔离"（`rls.py` 的设计原则）完全同源。

配套（第三十二条"开发测试运维分离"的工程化）：

| 要求 | 落地 |
|---|---|
| 环境隔离 | 三套 compose/K8s namespace；**不同数据库凭据**；网络分段（§9.4 L3） |
| 权限分离 | 开发角色**不得持有生产库凭据**；运维动作走 break-glass（§4.3.1） |
| 发布流程 | 镜像晋升（同一 image 从 test 升到 prod，不重新构建）+ 变更留痕 |
| 生产只读兜底 | 运维排查默认用只读账号（`moss_ro`，`rls.py` 已有角色定义） |

#### 8.7.3 当前 `manage.py` 的实测缺口（**"生产与调试是否分开"的答案：没有**）

> **需求问**："当前项目使用的 CLI 统一入口，看下能否实现后端单独测试调试？
> 生产环境和调试环境是否分开了？"
>
> **答：没分开。** 而且有一处"变量已存在但没人校验"的半成品 —— 这是最容易让人误判的地方。

| # | 实测缺口 | 证据 | 后果 |
|---|---|---|---|
| 1 | **`MOSS_ENV` 只在测试时设** | `manage.py:72` 的 `build_test_env()` 里有 `"MOSS_ENV": "test"`，但 `Settings` 没有 `env` 字段（§2.1），**`cmd_start` 也不设置、不校验** | 跑起来的生产进程 `MOSS_ENV` 为空；**没有任何东西能判断"我现在是不是生产"** → §8.7.2 的启动自检无从谈起 |
| 2 | **调试与生产共用同一份可写数据** | `data/moss_finagent.db`（实测 **6.4GB**）、`data/quant/`（31GB）、`configs/` 都是同一份 | 调试时改配置/写库 = **直接作用于线上**；调试产生的脏数据会留在生产库里 |
| 3 | **`--reload` 会全量重启** | `manage.py:457` `uvicorn --reload` 是"改文件即重启整个进程" | 开发时每次保存都让线上的 100 个连接断一次；**且**每次重启都要重新预热（见 §8.7.4） |
| 4 | **没有"第二个实例"的位置** | 端口 8100、SQLite 单写者、PID 文件、`data/run/*.log` 都是**单实例假设** | 想"起一个调试实例"必然与线上抢 SQLite 写锁 → `database is locked` |
| 5 | **前端构建会覆盖正在服务的目录** | `cmd_build` 把产物写进 `web/dist`，而运行中的服务正用 `StaticFiles(directory=web/dist)` 托管它（`api/main.py:408`） | 构建期间的几秒里，用户可能拿到**半更新的资源**；且 `index.html` 引用的旧 hash 资源若被删 → 用户报"白屏" |

#### 8.7.4 ★ 只有盘中才暴露的隐藏成本：**重启 ≠ 只断连接**

这是本需求**最容易被低估**的一点。现有代码对冷启动有明确实测：

| 场景 | 实测值 | 出处 |
|---|---|---|
| 冷启动首个 `/watchlist` | **59.8 秒** | `api/main.py:290` 注释 |
| 冷启动首个 `/intraday/watchlist` | **219.8 秒** | `service.py:1912` 注释 |
| 整表重算（50 只，冷） | **约 200 秒** | `service.py:1909` |
| 陈旧 WAL 导致的每请求穿透 | **182.9 秒** | `api/main.py:45` 注释 |

> **所以真实影响不是"断线几秒"，而是"断线后 1~3 分钟里所有人拿到的是空数据"**：
> 缓存空了 + 冷取要打数据源 + 而数据源正在被预热和 100 个用户同时抢。
> **对 09:20–10:30 这个窗口，一次重启的代价可能就是整个峰值期。**
>
> **结论：维护窗口不能靠"选个空闲时间"解决**（他们的空闲时间恰好是别人的盘中），
> **必须做到"新实例先热起来、再切流"**（§8.7.5）。

#### 8.7.5 方案 A：**后端单独测试调试**（不用等零停机，先能开工）

**目标**：开发时改代码不影响线上；调试实例与线上**数据完全隔离**。

```bash
# 现状（会打到线上）
python manage.py start --reload                       # ❌ 改文件即重启线上

# 目标：第二个隔离实例
python manage.py start --env dev --port 8101 --reload
```

**`--env dev` 要做的六件事**（都是在现有 CLI 上加分支，不改架构）：

| # | 动作 | 为什么 |
|---|---|---|
| 1 | 设 `MOSS_ENV=dev`；**若检测到 `MOSS_ENV=prod` 已设则报错退出** | 防"以为在调 dev，其实连了 prod" |
| 2 | 数据重定向：`SQLITE_PATH=data/dev/dev.db`、`SCHEDULER_DIR=data/dev/scheduler`、`LLM_AUDIT_DIR=data/dev/audit` | 复用现成的 `build_test_env()` 思路（它已经这么做了，只是只给测试用） |
| 3 | **数据源可切换为录播**：`DATA_SOURCE_MODE=replay\|live`；录播从 `data/recordings/` 读冻结行情 | ① 调试不消耗真实源配额；② **结果可复现**（与项目"冻结行情录像带做黄金回归"的既有做法同源） |
| 4 | **关闭全部定时任务**（`SCHEDULER_ENABLED=0`） | 否则调试实例会跑 `quant_data_sync`（16:40）→ **两个进程同时写 31GB 仓库** |
| 5 | **只绑 `127.0.0.1` + 独立 PID 文件**（`data/dev/run/dev.pid`） | 与线上实例互不干扰；`stop/status` 要能按 env 区分 |
| 6 | **日志分文件**（`data/dev/run/dev.log`） | 否则 `manage.py logs` 会把两个实例的输出混在一起，排查时无法区分 |

> ⚠️ **隔离的边界：应用库 ≠ 行情仓**（2026-09-23 实测踩坑，已修）。
>
> `MOSS_SQLITE_PATH` 是**应用库**（`fact_*` / 告警 / 做T权重档案）的开关，
> **不能**用它把**行情仓**（`data/quant/warehouse.db`，15~31 GiB 只读行情数据）一起指走。
>
> 曾经的形态：`src/quant/warehouse.py` 也认这个变量 → `manage.py --env dev`
> （`--env` 缺省就是 `dev`）起的实例读到一个几乎空的行情仓：
> **股票字典只剩 4 条**（只有按需补录过的那几只），
> "输入中文名 / 拼音首字母联想"整段失效 —— 用户报障「汇成真空 301392、
> 大亚圣象 000910 都识别不了」，同一原因还会让**资金流 / 流通市值 / 换手率**变空
> （都读行情仓）。
>
> 现在行情仓**只认 quant 专属开关**：`MOSS_QUANT_SQLITE`（或 `MOSS_QUANT_DB_URL` /
> `QUANT_DB_URL`）。确实要给 dev 实例一份隔离的行情仓，就**显式**设 `MOSS_QUANT_SQLITE`
> 指向副本 —— 而不是借应用库的开关顺手带跑。
> 单测钉住这条边界：
> `tests/unit/test_quant_warehouse.py::test_app_db_override_does_not_move_quant_warehouse`。
>
> 另注：dev 隔离保护 31GB 仓库的手段是**关掉全部定时任务**（上表第 4 条），
> 不是重定向它的路径 —— 这条边界与那个设计是一致的。

**前端侧的两条**（与 §8.7.3 缺口 5 呼应）：

| 项 | 现状 | 目标 |
|---|---|---|
| dev server | `manage.py frontend` 起 5173，**但 API 仍指向 8100（线上）** | dev 的前端必须指向 **8101（dev 后端）**；用 `VITE_API_BASE` 区分 |
| 构建 | 直接写 `web/dist`（正在被服务） | **构建到 `web/dist.new` → 原子替换**（`os.replace`，与项目写 YAML 的做法同源）；**旧 hash 资源保留 ≥1 天**（防用户拿着旧 `index.html` 404） |

#### 8.7.6 方案 B：**零停机维护**（目标态，与 §8.2 的进程拆分配合）

**核心思路：维护的对象不同，影响面就不同——先把"能热换的"和"不能热换的"分开。**

| 维护对象 | 现在的影响 | 目标做法 | 影响面 |
|---|---|---|---|
| **前端静态资源** | 构建期间可能拿到半更新资源 | 新版本写入 `dist.new` → 原子替换；`index.html` 设 `no-cache`（**已在做**，`api/main.py:398`）；旧 hash 资源保留 | **用户无感**（刷新即新版本） |
| **普通业务逻辑**（API/面板） | 重启 = 断线 + 冷启动 1~3 分钟 | 见下方"双实例交接" | 秒级（WS 断开+自动重连） |
| **做T快照/推送循环** | 同上 | ★ **拆进程后永不重启**（§8.2 的 `realtime` 进程保持长驻，只重启 `api`） | **零影响** |
| **行情取数/预热** | 重启后缓存空 | 缓存迁移（Redis）→ 新实例直接命中 | **零影响** |
| **数据库 schema** | 变更可能锁表 | 只做**向后兼容**的加列（`ALTER TABLE ADD COLUMN` + 默认值）；**绝不删列/改类型** | 零停机 |

**双实例交接（Blue-Green，四个阶段）**：

```
① 预启动：新实例起在 8102，**只绑 localhost、不接流量**
   ├ 复用同一份 Postgres/Redis → 直接命中已有缓存（**不冷启动**）★关键
   ├ 健康检查 /healthz + /ready 就绪后才进入下一步
   └ 若 SQLite 后端：**此阶段起不来**（单写者）→ 这就是 §10 P3 必须先做 Postgres 的原因

② 切流：反向代理把**新请求**指向 8102（reload 配置）
   ├ WebSocket 用 `code` 一致性哈希（§8.5.1）→ 新连接落新实例
   └ 旧实例**继续服务已建立的 WS**（不主动断）

③ 排空（drain）：给旧实例一个宽限期（默认 **到当个交易时段结束**，或最长 30 分钟）
   ├ 向旧实例的 WS 推 `{"type":"server_restart"}` + close code **1012 Service Restart**
   │   （前端按 §8.6.11.6 的退避**静默重连**到新实例）
   └ 宽限期内旧实例**只读、不再接受新连接**

④ 下线：旧实例 checkpoint + 优雅退出（复用现有 `request_graceful_stop` 路径）
```

> ⚠️ **三个必须遵守的前置条件**（否则 Blue-Green 只是"换个姿势重启"）：
> 1. **共享状态必须外置**（Postgres + Redis）。SQLite 单写者下**第二个实例根本起不来** ——
>    所以 §10 的 **P3（Postgres 迁移）不是"以后再说"，而是零停机的前置条件**。
> 2. **缓存必须能跨实例命中**，否则新实例 = 冷启动 = 1~3 分钟空数据（§8.7.4）。
> 3. **WS 必须能优雅重连**：close code 用 **1012**（Service Restart）而不是 1000，
>    前端据此**立刻重连**而不是走"网络断了"的长退避。

**`manage.py` 上的落点（三个新命令，不推翻现有结构）**：

```bash
python manage.py start --env prod                 # 生产（校验 MOSS_ENV，跑 §8.7.2 自检）
python manage.py start --env dev  --port 8101     # 隔离调试实例（数据/调度/日志全隔离）
python manage.py deploy --blue-green              # 预启动→健康检查→切流→排空→下线
python manage.py stop  --env dev                  # 按 env 精确停止（PID 文件分开）
python manage.py status --all                     # 列出所有 env 的实例
```

**一个刻意的取舍**：本机单机部署时，反向代理是必需项（现在是 uvicorn 直连 8100）。
最小代价是 **Caddy**（一个二进制、配置 3 行、**reload 配置不断连接**），
与 §7.5 的 Cloudflare Tunnel 串起来就是：

```
Cloudflare Tunnel ──► Caddy :8100 ──┬─► api-blue  :8102
                                    └─► api-green :8103     （切流只改 Caddy 配置）
```

### 8.8 负载均衡与容灾

#### 8.8.1 负载均衡（"无状态接入 + 有状态收敛"）

| 层 | 做法 | 依据 |
|---|---|---|
| **接入层** | Nginx/Caddy：HTTP 轮询；**WS 按 `code` 一致性哈希**（同标的粘到同一副本，减少重复订阅） | §8.5.1 |
| **会话** | 令牌无状态（JWT/HMAC）+ `fact_session` 记录 `jti` 用于吊销；**不依赖粘性会话** | §8.5.2 第 9 条 |
| **计算** | 接入无状态可水平扩；**计算按标的收敛**（活跃宇宙是全局单例） | §7.2 |
| **配额** | 按 `(tenant, user)` 限流（§4.6）+ 域名级令牌桶（§7.2.6） | moss 的 `rbac_policy.json` 每角色带 `rate_limit_per_min` / `max_rows_per_query`，**直接借鉴这个"权限 + 限流写在同一张表"的形式** |
| **健康检查** | `/healthz` 公开（探针）；`/ready` 检查 DB/Redis/数据源可达性；**不健康先摘流量再重启** | 现有 `/api/v1/metrics/ready` |

#### 8.8.2 容灾分级（按 RTO 排，不追求"全都高可用"）

| 组件 | 故障影响 | 容灾手段 | RTO 目标 |
|---|---|---|---|
| **行情数据源** | 该维度不可用 | 多源降级链（已实现）+ 异常上报（§7.6.3）+ 前端明示 | **0**（自动切换） |
| **Redis** | 缓存失效、锁失效 | 降级为进程内缓存 + **fail-closed 限流**（§8.5.2 第 5 条）；不做 Redis 主从（单机够用） | < 1min |
| **Postgres** | 个人数据不可读写 | 每日全备 + WAL 归档；**先做恢复演练**（比买高可用更值）；只读副本可选 | < 30min |
| **API 进程** | 部分用户断连 | 多副本 + 反代摘除；WS 客户端指数退避重连 | < 30s |
| **realtime 进程** | 无实时推送 | 前端自动降级为 20s 轮询（**现有 `POLL_FALLBACK_MS` 机制，保留**） | < 5s |
| **scheduler** | 定时任务不跑 | 手动触发接口（现有 `/scheduler/jobs/{name}/run`）兜底 | 人工 |
| **qmt-sidecar** | 该源不可用 | 已在链尾兜底，腾讯顶上 | 0 |
| **整机/机房** | 全部不可用 | 内部系统，RPO/RTO 未到跨机房要求 → **先做备份与演练**（§8.5.5 已列为"不做跨机房"） | 小时级 |

> **诚实边界**：上表里**没有一项是"已实现"**——除"数据源多源降级"与"前端轮询兜底"
> 是现有代码外，其余都是设计。**不要把这张表说成已完成。**

#### 8.8.3 灾备演练（第三十九条要求"应急演练"，报告留存 ≥5 年）

| 演练 | 频率 | 通过标准 |
|---|---|---|
| 数据源全链失败（断网模拟） | 每季 | 面板显示"数据不可用"而非假数字；异常上报落库；恢复后自动回正常源 |
| Redis 宕机 | 每季 | 服务不 5xx；降级为进程内单飞 + 限流；恢复后自动重连 |
| Postgres 恢复演练 | 每半年 | 从备份 + WAL 恢复到指定时间点；**核对个人数据完整性**（抽样比对） |
| 单模块崩溃 | 每次发版 | `kill` worker 进程 → 实时链路与其它模块**无感知**（隔离验证） |
| 峰值压测 | 每次大改 | 模拟 9:25 洪峰（100 WS + 100 首屏），p95 达标、无 5xx（§9.2） |

演练记录落 `docs/`（本项目文档入库）并注明保存期 —— 对应**第三十九条（≥5 年）**。


---

## 九、可观测性与验收

### 9.1 必须新增的指标

| 指标 | 为什么 |
|---|---|
| `snapshot_inflight{code,cap}` / `snapshot_cache_hit{cap}` | 验证 §7.1 是否真的共享了 |
| `snapshot_latency_p95` / `compute_queue_depth{kind}` | 排队是否失控 |
| `per_user_qps{tenant,user}` / `per_tenant_tokens` | 配额与"谁在刷" |
| `cache_key_scope_errors` | 缓存作用域错配的**告警**（§9.3） |
| `heavy_job_active{kind}` | 重活是否在偷实时的时间 |
| `auth_version_lag{replica}` | 权限版本号落后程度（§8.5.2 第 3 条是否生效） |

### 9.2 验收标准（CI 必须覆盖）

**隔离类**

1. `test_user_isolation_watchlist`：A 加自选，B 的 watchlist 不含该票。
2. `test_user_isolation_profile`：A 改 300308 权重，B 读到的仍是自己的（或全局默认）。
3. `test_fit_cache_keyed_by_caliber`：A、B 权重不同 → 两次 `level_fit` 结果不同；
   权重相同 → 只算一次（**缓存命中数 == 1**）。
4. `test_tenant_isolation`：跨租户读任一 L1/L2 资源 → 403。
5. `test_capability_denied`：无 `quant.selector.run` → 403；能力码未登记 → 403。
6. `test_alert_pref_per_user`：A 的渠道只推 A 的池子。
7. `test_rls_policy_not_noop`：断言生成的策略 SQL 含 `user_id =`。
8. `test_cache_key_has_user_or_caliber`：**AST 级检查** —— 所有缓存写入点的键
   要么不含用户相关输入（L0），要么含 `user_id`/口径指纹。
9. `test_grant_cannot_self_approve`：同一人既申请又批准 → DB 层拒绝。
10. `test_mutually_exclusive_roles`：同时授予互斥能力对 → 拒绝（§4.3.1）。

**性能类**

11. `test_snapshot_singleflight`：同 `(code, 口径)` 并发 20 → 实际计算次数 == 1。
12. `test_100_user_smoke`（脚本）：100 路 WS + 100 人列表刷新 5 分钟，
    断言 `p95 < 1.5s`、`compute_queue_depth < 50`、无 5xx。
    复用 `scripts/_probe_concurrency.py`（改造前后各跑一次对比）。

**合规类**

13. `test_audit_has_user_and_tenant`：每条审计含 `user_id`/`tenant_id`/`action`/`chain_hash`。
14. `test_four_eyes_enforced`：导出/建租户/接数据源/训练模型无 `approver` 时**不能执行**。
15. `test_restricted_symbol_blocks_write`：限制名单内标的 → 写操作 403（§4.4）。
16. `test_registration_requires_approval`：`pending` 用户**登录被拒**且返回明确原因；
    管理员批准后才可登录；**申请人自批被拒**（§8.6）。
17. `test_prod_config_selfcheck`：`env=prod` 时用 SQLite / localhost DB /
    未开强制鉴权 / 开着请求头身份 → **启动即失败**（§8.7.2）。
18. `test_review_flow_is_audited`：审批流水落 `fact_registration_review` 且哈希链可验证（§8.6）。
19. **`test_valid_until_enforced`**：`valid_until` 过期后**新登录被拒**；
    已在线的会话在**当个时段结束后**才断（不是立即踢）；续期后恢复正常（§8.6.1.2）。
20. **`test_no_permanent_account`**：任何创建路径都不允许 `valid_until IS NULL`
    （含管理员新建、CSV 批量导入）→ DB 层 `NOT NULL` 兜底（§8.6.1.2）。
21. **`test_soft_delete_keeps_audit_chain`**：软删除用户后，
    ① 他的审计记录**仍在**且哈希链**可验证**；② 列表默认不显示；
    ③ `restore` 后一切恢复（§8.6.1.3）。
22. **`test_anonymize_preserves_audit`**：匿名化后 `email/phone` 已抹除，
    但 `user_id` 与业务归属关系**不变**、审计链**仍然可验证**（§8.6.1.3 L3）。
23. **`test_admin_cannot_modify_self`**：管理员修改自己的层级/配额/有效期 → 被拒（§8.6.10.3）。
24. **`test_quota_override_requires_expiry`**：建个人配额覆盖不带 `expires_at` → 被拒（§8.6.10.5）。
25. **`test_deleted_user_email_reusable`**：软删除后同一邮箱可重新注册（部分唯一索引生效）（§8.6.1）。

**健壮性类（对应"子功能崩了不要整站崩"）**

26. `test_module_degradation_matrix`：**逐个**让某维度取数失败（板块 / 估值 / 消息面 /
    情绪周期 / 海外映射），断言：① 该维度 `available=False` 且进 `gaps`；
    ② **其余维度仍然出数**；③ 接口**不返回 5xx**（§7.6.1 的 `return_exceptions=True` 契约）。
27. `test_source_failover_and_anomaly`：主源全失败 → 自动切备用源；
    **全部源失败** → ① 写 `fact_source_anomaly`；② 数据置 `None` 而非 0；
    ③ **绝不回退 simulated**（§7.6.3）。
28. `test_domain_level_cooldown`：同一域名连续失败达阈值 → **该域全部方法**进入冷却，
    后续请求不再逐方法白等（§7.6.3 缺口①）。
29. `test_single_module_crash_isolated`（集成/脚本）：`kill` worker 进程 →
    实时链路与其它模块无感（§8.8.3 演练项的自动化版）。
30. `test_peak_window_load`（脚本）：模拟 **09:25–09:35 洪峰**（100 路 WS + 100 首屏
    + 盘前预热完成为前提），断言 p95 达标、无 5xx、`fetch_rate` 不超闸门（§7.2.5）。
31. `test_preheat_covers_universe`：09:15 预热启动后，09:30 时宇宙内标的（F+H 档）的
    5mK/分时**均为当日**（`trade_date` 校验），即"开盘后零冷取"（§7.2.5a）。
32. `test_watchlist_limits_three_levels`：池数 / 单池 / **总数（去重）** 三重校验都生效；
    同一只票放进 5 个池**不**增加总数（§7.2.6①）。
33. `test_custom_sector_not_in_universe`：把票加进自定义板块**不**使其进入实时宇宙
    （§7.2.6⑤ 的决策）。
34. **`test_quotes_refresh_uniformly_5s`**：报价快车道对**全宇宙所有自选池**的票
    统一 5s 刷新（**不按层级分档**）；试用用户与 VIP 用户看到的涨跌幅新鲜度**一致**（§7.2.6④）。
35. **`test_quotes_shared_fetch_per_user_distribution`**：验证"取数共享、分发按用户"——
    ① 100 个用户同时在线时，数据源侧**只发生 N 次批量请求**（N 与用户数无关，只与宇宙大小有关）；
    ② 但每个用户收到的 WS 帧**只含他自己池子的票**（不串号、不合并）（§7.2.6④b）。
36. **`test_pool_level_foreground_switch`**：用户从自选池切到自定义板块 →
    ① 该板块的票**立即升到 P0 档**（60s）；② 原自选池**降为 P1 档（不是停）**；
    ③ 切换瞬间**先出缓存数据、不白屏**，1~2 秒内分时补齐（§7.2.6⑤b/⑤c）。
37. **`test_cold_sector_gated_by_slack`**：模拟洪峰（slack 低）→ P2 冷板块**暂停刷新**；
    洪峰结束后 slack 高 → **自动补刷**（余量门控生效，§7.2.6⑤c）。
38. **`test_cf_header_stripping`**：请求携带伪造的 `X-Internal-Call` / `X-Tenant-Id` /
    `X-User-Id` / `X-Roles` ↔ **中间件必须全部剥掉**（不得凭它们提权）（§7.5 坑 4）。
39. **`test_cf_real_ip_recorded`**：审计里记录的是 `CF-Connecting-IP`，
    且**不信任** `X-Forwarded-For` 的首段（可伪造）（§7.5 坑 2）。

**认证类（§8.6，身份系统是全系统风险最集中处）**

40. **`test_login_no_account_enumeration`**：不存在的账号与密码错误 →
    **响应文案与状态码完全一致**，且**响应时间差 < 50ms**（防时序侧信道）（§8.6.5②）。
41. **`test_failed_login_locks_account_not_ip`**：连续失败 N 次 → **账号**被临时锁定；
    换 IP 仍锁定；**另一个账号不受影响**（证明计数按账号记）（§8.6.5②）。
42. **`test_remember_token_rotation_and_replay`**：① 每次用 remember token 都换发新的；
    ② **重放**旧 token → **整个 family 被撤销** + 强制重新登录 + 产生告警（§8.6.5④）。
43. **`test_password_change_revokes_other_sessions`**：改密后其它设备的 access/refresh
    **全部失效**，但**当前会话仍有效**；同时撤销全部 remember token（§8.6.5③）。
44. **`test_password_reset_revokes_all_and_alerts`**：重置成功后所有会话失效 +
    发短信告警到绑定手机号 + 写审计（§8.6.5⑥）。
45. **`test_reset_requires_sms_code`**：不带/带错验证码 → **拿不到重置令牌**；
    重置令牌**一次性**（第二次用失败）、**15 分钟过期**（§8.6.5⑥）。
46. **`test_phone_bind_requires_old_phone`**：换绑只验新号 → **拒绝**；
    换绑成功后**必须**向旧号发通知（§8.6.5⑤）。
47. **`test_email_not_enumerable`**：找回密码接口对"已注册"与"未注册"**邮箱**
    返回**同一文案**，且都在**同一数量级耗时**内返回（本期通道是邮箱）（§8.6.7 第 2 条）。
47b. **`test_email_deliverability_hint`**：注册/找回页面**提示"未收到请检查垃圾邮件"**；
     且"重新发送"在限流内**不消耗额外配额**（§8.6.5⑥ 邮件特有提醒）。
48. **`test_sms_rate_limit_three_layers`**：分别命中 手机号(1/分) / IP(5/时) / IP段(50/时)
    三层限流；**未过图形验证码不允许发短信**；验证码 5 分钟过期、错 5 次作废（§8.6.7 第 3 条）。
49. **`test_secrets_only_hashed`**：断言 `fact_verification_code.code_hash` /
    `fact_password_reset.token_hash` / `fact_remember_token.token_hash`
    **均不等于明文**；且代码里**没有**任何把验证码写日志的路径（§8.6.7 第 4 条）。
50. **`test_password_history_blocks_reuse`**：改回最近 5 个用过的密码 → 拒绝（§8.6.5③）。
51. **`test_console_sms_sender_blocked_in_prod`**：`env=prod` 且 SmsSender 是
    `ConsoleSmsSender` → **启动即失败**（与 §8.7.2 同源）（§8.6.8）。
52. **`test_session_list_and_logout_all`**：`/auth/sessions` 能列出活跃设备；
    `/auth/logout-all` 能一次踢掉全部（用户自救通道）（§8.6.6）。

**会话与 Cookie 类（§8.6.11）**

53. **`test_session_slides_but_absolute_cap_holds`**：
    ① 持续活跃 → `idle_expires_at` 不断后延；② 但到达 `absolute_expires_at`（12h）后
    **无论多活跃都必须失效**；③ **心跳/WS ping 不续期**（§8.6.11.3）。
54. **`test_reopen_within_refresh_window_no_password`**：关浏览器 → session cookie 丢失 →
    带 refresh cookie 重开 → **静默换新 session、不要求密码**（需求 21 的核心验收）（§8.6.11.3）。
55. **`test_session_id_isolation`**：同一用户**两个 session** →
    ① 会话态（当前池/当前标的/草稿/筛选）**互不可见**；
    ② 但**自选池与个股参数**（L2 用户数据）**两边一致**（不能误隔离）（§8.6.11.4）。
56. **`test_session_revoke_closes_ws`**：撤销某 session → 它的**全部** WS 连接立即关闭，
    且不产生新的推送（不留悬挂连接）（§8.6.11.4）。
57. **`test_multi_tab_shares_one_session`**：同一浏览器开 3 个标签页 →
    **同一个 `session_id`**、WS 订阅**共享**、宇宙取数**不重复**（§8.6.11.5）。
58. **`test_two_devices_independent`**：手机与电脑各自一个 session；
    **后者登录不踢前者**；`/auth/sessions` 能看到两台，可单独踢（§8.6.11.5）。
59. **`test_market_cache_not_session_scoped`**：断言行情/因子/打分结果的缓存键
    **不含 session_id**（防"过度隔离把命中率打没了"）（§8.6.11.4 表）。
60. **`test_expired_session_silent_refresh`**：access 过期 → 前端**静默 refresh 成功**
    （用户看不到"已登出"闪屏）；refresh 也失效 → 才跳登录页（§8.6.11.5）。

**通知通道与配额类（§8.6.8）**

61. **`test_notify_channel_email_only_this_phase`**：本期通道裁决**只走邮箱**；
    未验证邮箱 → **不允许注册成功/找回**（明确报错，不静默失败）（§8.6.8④）。
62. **`test_no_account_without_verified_email`**：注册时邮箱未验证 → **拒绝**；
    后台"无已验证找回通道账号"检查项为 0（§8.6.8④ 纪律 1）。
63. **`test_notify_quota_blocks_with_explicit_status`**：租户当月邮件额度耗尽 →
    返回 `status='quota_exceeded'` + 前端明示；**不产生"假装成功"的记录**（§8.6.8⑥）。
64. **`test_notify_cost_reconciles`**：`fact_notify_usage.cost_yuan`
    与 `fact_notify_log` 逐条 `unit_price` 累加**一致**（本期 email 单价为 0，仍要能累加）（§8.6.8⑤）。
65. **`test_console_notifier_blocked_in_prod`**：`env=prod` 且通道是 `ConsoleNotifier`
    → **启动即失败**（与 §8.7.2 同源）（§8.6.8⑥ 纪律 1）。
65b. **`test_sms_channel_is_pluggable`**：本期**不实现短信**，但断言
     ① `dim_user_contact` 支持 `kind='phone'`；② 通道裁决函数在"配置了 sms 且手机已验证"时
     会走短信（用 FakeNotifier 验证）；③ **加短信不需要改表、不需要改流程**（§8.6.8④）。
66. **`test_session_expiry_ws_control_frame_and_code`**：会话到期 →
   ① 服务端先推 `{"type":"auth_expired","reason":"session_expired"}`；
   ② 用 **close code 4001** 关闭（**不是 1000**）；
   ③ 前端 refresh 成功后**自动重连且不提示用户**（§8.6.11.6 要点①②）。
67. **`test_disabled_account_ws_closed_immediately`**：管理员禁用账号 →
   该用户**全部** WS 在秒级内关闭（`4002`），**无宽限**；且此后**不再收到任何数据帧**（§8.6.11.6）。
68. **`test_expired_account_ws_grace_readonly`**：账号到期 →
   ① 宽限期内连接**保留**但**不再推送信号/提醒**、面板为只读；
   ② 宽限结束用 `4003` 关闭；③ 续期后恢复（§8.6.11.6）。
69. **`test_ws_close_code_not_1000`**：断言三种失效**各自使用不同的 4xxx code**；
   若统一用 1000 → 测试失败（防"前端以为网络断了 → 无限重连"）（§8.6.11.6 要点②）。
70. **`test_revoke_stops_push_server_side`**：撤销 session 后，
   推送循环**服务端侧**就不再取该 session 的数据（即使客户端忽略关闭帧也收不到）—— 
   订阅表按 `session_id` 索引来保证（§8.6.11.6 要点③）。
71. **`test_refresh_single_flight`**：同屏 5 个请求同时收到 401 →
   只发出**一次** refresh 请求，其余共享其结果；
   **不得**因并发 refresh 触发"令牌重放检测"而撤销用户自己的会话（§8.6.11.6 前端细节）。
72. **`test_kick_broadcast_cross_process`**（多副本场景/集成）：在副本 A 禁用账号，
    挂在副本 B 上的 WS **在 1 秒内**关闭（经 Redis 广播，不是等下次鉴权）（§8.6.11.6 要点④）。

**环境隔离与维护类（§8.7.3~8.7.6）**

73. **`test_env_required_and_validated`**：不设 `MOSS_ENV` → **拒绝启动**；
    `env=prod` 但 SQLite/localhost/未开强制鉴权 → **拒绝启动**（§8.7.2、ADR D37）。
74. **`test_dev_instance_isolated_paths`**：`--env dev` 启动后断言
    `SQLITE_PATH` / `SCHEDULER_DIR` / `LLM_AUDIT_DIR` / PID 文件 / 日志
    **全部落在 `data/dev/` 下**，且**不打开生产主库**（§8.7.5 六件事）。
75. **`test_dev_disables_scheduler`**：`--env dev` 时 `quant_data_sync` 等定时任务**全部不注册**
    （防两个进程同写 31GB 仓库）（§8.7.5 第 4 条）。
76. **`test_replay_datasource_mode`**：`DATA_SOURCE_MODE=replay` 时**不发出任何真实外网请求**
    （用 monkeypatch 断言零出网），且同一录像带两次运行结果**逐位一致**（§8.7.5 第 3 条）。
77. **`test_frontend_build_atomic_swap`**：构建过程中 `web/dist` 始终是**完整可服务**的；
    替换是原子的；旧 hash 资源**仍可访问**（不 404）（ADR D39）。
78. **`test_blue_green_cache_warm_no_gap`**：Blue-Green 切流后
    **首个请求命中共享缓存**（不出现 59.8s / 200s 级冷启动）（§8.7.6 前置条件 2）。
79. **`test_ws_reconnect_on_1012_not_backoff`**：收到 **1012 Service Restart** →
    前端**立刻重连**（不走"网络断了"的长退避）；100 个客户端重连**带抖动**（§8.7.6 阶段③）。

### 9.3 两个"防自欺"的哨兵（本项目的既有风格）

1. **缓存作用域哨兵**：开发模式下，任何 L2 数据的缓存 key 若不含 `user_id`
   或口径指纹，**打 ERROR 日志并计数**（不静默）。这类 bug 的表现是
   "偶尔看到别人的分数"，极难复现——必须让它在测试环境就吵。
2. **假隔离哨兵**：`rls.py` 的上一版曾是"取了租户、遍历了表、然后 return stmt"的
   **假实现**（文档里已自曝）。新加的用户级守卫必须有
   "断言 SQL 片段非空且含 user_id"的单测——**同一类错误不许犯第二次**。

### 9.4 控制台隔离：研究环境与投资环境的物理/逻辑分离

《信息隔离墙制度指引》**第十三条**要求"冲突业务的**信息系统相互独立或实现逻辑隔离**"，
第十五~十七条要求跨墙交流须**审批**、跨墙结束要**回墙**并留痕。

**为什么不能只靠"角色不同"**：同一个人、同一个页面、同一套凭证，
只靠"他点不到那个按钮"来实现隔离 —— 这在合规上站不住
（第十三条要的是**系统层面**的独立或逻辑隔离，不是 UI 层面的隐藏）。

| 阶段 | 做法 | 隔离强度 | 成本 |
|---|---|---|---|
| **L1 逻辑隔离（本期建议）** | 同一进程，但**两套入口路由 + 两套凭证受众**：`/research/*` 与 `/invest/*` 分别校验**不同的能力码集合**；研究结论默认 `private`，进入投资侧必须显式**跨墙工单**（`WallCrossing`，已有实现） | 中 | 低（复用现有 `WallGroup`/`WallCrossing`） |
| **L2 会话隔离** | 同一用户在两套控制台**分别登录、各自 session**（`fact_session.console`），令牌**不可跨控制台复用** | 较高 | 中（加一列 + 中间件校验受众） |
| **L3 环境隔离** | 两套部署（不同域名/端口/数据库角色），跨环境取数走审批工单，**工单本身即合规记录** | 高 | 高 |

> **本项目的现实取舍**：当前是**单用户桌面版**，没有"研究/投资"两种人。
> 所以**本期只做 L1 的骨架**（能力码分组 + 现有 `WallCrossing` 接线），
> L2/L3 写进设计但**不实现** —— 面试时把"我判断本期做到哪一层、为什么"
> 说清楚，比硬说"我做了物理隔离"更可信。
>
> **可引用的一句话**：*"隔离墙的落地不是加个角色，而是
> '系统层面独立或逻辑隔离'（指引第十三条）——
> 我把它落成两套能力码受众 + 跨墙工单留痕；物理隔离留给真的有多部门部署需求时再做。"*

---

## 十、迁移与实施顺序（每阶段都可独立上线）

| 阶段 | 内容 | 产出 | 不可跳过的理由 |
|---|---|---|---|
| **P0（1~2 周）** | 用户/租户/角色/能力**表 + 登录 + `/me/*`**；**注册审批流（§8.6：pending 不可登录 + 审批流水）**；**三环境开关与启动自检（§8.7）**；**`dim_tier` 三档（管理员100/VIP20/试用10）与服务端上限校验**；`dim_intraday_profile_v2` 建表并双写（**copy-on-write**：有自定义才落库）；`dim_user_watchlist` 建表 + YAML 导入脚本；前端带 token | 能登录（含审批）、能按人存自选与权重、套餐上限生效、环境不会连错 | 没有"用户"这一级与审批，后面全是空转；上限不在服务端校验等于没有；环境串了会污染生产数据 |
| **P1（1~2 周）** | 做T模块**全部缓存加用户/口径维度**；`_push` 按用户；**健壮性：降级矩阵 + 域级冷却 + 异常上报（§7.6）**；隔离测试 1~10、健壮性测试 19~21 通过 | 做T模块可多用户共存，且单维度故障不整站崩 | 这阶段的 bug 会直接导致"看错别人的分数"→ 误导交易 |
| **P2（2~3 周）** | **活跃宇宙调度 + 盘前预热（§7.2.5）**：全局刷新循环按标的取数、分档 TTL、并发闸门；单飞 + 共享因子层 + `fact_job` 队列 + 重活移出 API 进程 | 取数去重、峰值洪峰有预案、并发能力 5/s → 50/s+ | 决定"100 人能不能用"；**取数与预热必须早于人数增长**（否则先被数据源限流 / 开盘即雪崩） |
| **P3（1~2 周）** | Postgres 迁移（含 RLS DDL 与连接池会话变量）+ Redis 锁；`scheduler`/`worker` 拆进程 | 可多副本 | SQLite 单写者下多副本必 `database is locked` |
| **P4（1 周）** | Nginx + TLS + 限流 + 配额 + 审计查询页 + 压测 | 能远程发布 | 远程暴露前必须有关闭鉴权之外的保护 |
| **P5（持续）** | 其余模块逐个纳入用户维度 | 全平台一致 | 分批可回滚 |

**回滚策略**：P0/P1 的双写期（YAML 与 DB 同时写）保留 ≥ 2 周；
`dim_intraday_profile` 旧表**不删**，直到 P1 验收通过并观察一个完整交易日。

---

## 十一、关键决策记录（ADR）

| # | 决策 | 备选 | 理由 |
|---|---|---|---|
| D1 | **共享表 + `tenant_id`/`user_id` 列** | 每租户 schema / 独立库 | 100 用户 / 10 租户 / 个人数据 < 1 万行。**有规模判据**：Citus 给出"5~50 租户可用独立库；上千租户须共享表分片"，本项目落在"独立库可行"区间**边缘**，但**数据分层后结论反转**——31GB 行情/因子必须共享，私有数据独立与否无所谓 |
| D2 | **不引入 Zanzibar/OpenFGA 式 ReBAC** | 关系元组授权 | 资源层级只有三级且固定，外部授权服务的运维成本 > 收益。**但承认两点**：① 资源确为树形归属，ReBAC 本可自然表达；② **借用其两个结论**——撤权延迟用权限版本号（zookie 思想）、共享档位用 relation 化三档 |
| D3 | **能力码走 `authorize()` 扩展，不新建第二套引擎** | 独立 CapabilityService | `policy.py` 的"单一入口"是明确设计原则；两套引擎必然漂移 |
| D4 | **`user_id` 是一等隔离维度** | 把"个人"当特殊租户 | 一人一租户会让"部门共享"退化，且租户表会爆炸 |
| D5 | **保留 YAML 作为平台默认模板** | 全量迁 DB | 平台级默认值用文件可版本化/可评审；用户的个性化才进 DB |
| D6 | **本期一个用户一个租户** | Principal 支持多租户 | 避免每个查询变 `IN (...)`；多部门场景用账号映射 |
| D7 | **先共享计算，再谈多副本** | 直接水平扩 | `实测`瓶颈是"每请求各算一遍"，不是副本数；不修这个，扩副本只是线性复制浪费 |
| D8 | **QMT 在单进程内独占** | 多进程直接调 xtquant | 历史事故：非线程安全原生下载接口导致进程无 traceback 猝死 |
| D9 | **审计双写**（JSONL 哈希链 + DB 查询副本） | 只留其一 | JSONL 满足"不可篡改+独立存储"；DB 满足分页检索；二者权限与介质应分离 |
| D10 | **高权操作走 break-glass 临时提权** | 常驻高权账号 | 办法第三十二条（最小权限 + 岗位相互制衡）+ NIST SP 800-53 AC-5；实现 = 两张表 + 强制到期 + DB 层禁止自批 |
| D11 | **控制台隔离本期只做 L1（逻辑隔离）** | 直接上两套部署 | 当前无"研究/投资"两种人；指引第十三条只要求"相互独立**或**逻辑隔离"，L1 已满足合规最低线 |
| D12 | **注册必须管理员审批（`pending` 不可登录）** | 自助注册即可用 / 邮箱验证即用 | 指引第八条"需知原则"：**谁能进系统本身需要审批**；金融场景下自助开通是合规缺口（§8.6） |
| D13 | **三环境分离 + 启动自检拒绝不自洽配置** | 只靠"约定别连错库" | 办法第二十一条（独立测试环境）+ 第三十二条（开发测试运维分离）；**"拒绝启动"比"打警告"唯一 100% 会被看见**（§8.7.2） |
| D14 | **状态隔离用 Actor 模式，但只对有竞态的状态用** | 全部 Actor 化 / 全部裸锁 | 借鉴 `moss-finance-assistant` 的 Actor 基类：状态修改收敛到单函数、单条消息失败不影响循环；但**全盘 Actor 化会让所有访问异步化 + 邮箱积压**，只对 `SourceHealthTracker` / `_watch_cache` 等有竞态的状态用（§7.6.2） |
| D15 | **降级必须让用户看得见**（报价 5s→15s、维度不可用都要标出来） | 静默降级 | 静默把 5 秒变 15 秒会让用户以为"行情就是不动"，是**会误导交易**的（§7.2.5b） |
| D16 | **有效期到点不硬切断**：拒绝新登录，已连会话在当个时段结束后再断 | 到点立即踢出 / 不做到期 | 盘中 10:00 把人踢出去比没有有效期更糟；但**永久账号也不允许**（`valid_until NOT NULL`，永久须 break-glass 留痕）。（§8.6.1.2） |
| D17 | **删除只能是软删除 + 匿名化，不删审计与业务数据** | 硬删除用户行 | 审计是哈希链（删一条后面全对不上）；《证券法》第二百一十四条对篡改毁损资料有罚则；办法第十六条要求审计报告留存 ≥20 年。**匿名化 ≠ 删行**（§8.6.1.3） |
| D18 | **管理员不能改自己**（层级/配额/有效期） | 允许自助 | 自我提权/自我续期是内部平台最常见的越权路径；与 §4.3.1 禁止自批同源（§8.6.10.3） |
| D19 | **个人配额破例必须有到期时间** | 允许永久破例 | 否则"临时特殊"变"永久特权"且无人记得原因；与 break-glass 强制到期同一原则（§8.6.10.5） |
| D20 | **报价不分档（全宇宙统一 5s），只有逐只接口分档** | 报价也按 F/H/L 或按付费层级分档 | `实测`批量快照一次可带 **300~500 只**且耗时几乎不随数量增长（62ms→96ms），全宇宙 5s 的成本仅 **2 req/s / 1% 单核**。**按层级给报价分档是无谓的体验落差**；稀缺的是逐只接口（分时/5mK），那才按 F/H/L 分档（§7.2.6④） |
| D21 | **"取数共享"与"分发按用户"必须显式分开** | 混为一谈 | 混了会写出两种错：① 共享=只推一份 → **数据串号**（用户看到的不是自己的池子）；② 分发=各取一次 → **100 倍重复请求**（必被限流）。正确：取数全局去重，分发按用户切片（§7.2.6④b） |
| D22 | **"宇宙"与"实时集"解耦；每用户最多 1 个池在实时档** | 所有池都实时 / 板块永不刷 | 前者 = 100 人 × 21 池 = 数万只实时（必被限流）；后者 = 用户切到板块看到十几分钟前的价（会误导交易）。**实时集只跟"在看什么"有关**，与"有多少池"无关（§7.2.6⑤b） |
| D23 | **冷池用"余量门控"刷新，不用固定定时** | 固定 15 分钟定时刷 | 固定定时会在**开盘洪峰时准时开跑** P2 —— 那正是最不该抢资源的时候。余量门控让它**自动避峰**：洪峰停、盘后补（§7.2.6⑤c） |
| D24 | **Cloudflare Tunnel 只解决"怎么进来"，不解决"谁能进来"** | 以为有隧道就安全 | 隧道**不提供任何认证**；公网任何人可达源站 → `MOSS_TENANCY_ENFORCE=1` 与关闭请求头身份是硬要求，且**必须在中间件最前面剥离** `X-Internal-Call`/`X-Tenant-Id`/`X-User-Id`（隧道不做头剥离）（§7.5） |
| D25 | **"记住我"绝不存密码**：长期 refresh token + 轮换 + 重放检测 | 服务端存可逆密码 / 只发一个长期 token 不轮换 | 存可逆密码 = 泄露即全损；不轮换的长期 token 一旦被窃就是**30 天静默后门**。收到已轮换的旧 token → 判定重放 → **撤销整个 family**（§8.6.5④） |
| D26 | **找回密码的语义只能是"手机号 + 短信验证码"** | 字面实现"手机号 + 密码找回" | 知道密码就直接登录了，"找回"无意义。正确流程 = 短信验码 → 一次性高熵重置令牌（存哈希、15 分钟、用后即废）→ 设新密码（§8.6.5⑥） |
| D27 | **短信接口必须三重限流 + 前置图形验证码** | 只按手机号限流 | 短信是**真金白银**（¥0.03~0.05/条）且会**轰炸真实用户**。必须 `(手机号 1/分, 10/日)` + `(IP 5/时)` + `(IP 段 50/时)`，且发短信前先过图形码（§8.6.7） |
| D28 | **改密/重置后必须撤销其它会话；换绑手机号必须验旧号并通知旧号** | 只改密码不管会话 / 换绑只验新号 | 用户改密的第一动机就是"怀疑被盗"，不踢会话等于没改；只验新号 → 攻击者换绑后**永久接管账号**（因为找回走手机号）。通知旧号是用户**唯一可能察觉**盗号的时机（§8.6.5③⑤） |
| D29 | **两层凭证**：`session cookie` 管"这一会儿"（30 分钟滑动 + **12 小时绝对上限**），`refresh cookie` 管"这台设备"（🔒 **7 天滑动，已确认**） | 单层长 Cookie / 单层短 Cookie | 单层长 = 会话永不失效（笔记本丢了等于账号丢了）；单层短 = 用户每 30 分钟被要求重新登录（需求明确不要）。**绝对上限不能省**，否则被窃 cookie 持续请求就永不失效（§8.6.11.1） |
| D30 | **会话锚点用不透明 `session_id`，不用 JWT**（access token 仍可 JWT） | 会话直接放 JWT | 需求要的是**会话级隔离**，那就必须能**立刻踢掉某一个会话**；JWT 无法即时吊销。不透明 ID + `fact_session` 查表才能做到精确撤销（§8.6.11.2） |
| D31 | **sessionId 只隔离"会话态"，不隔离市场数据与用户数据** | 所有缓存都加 session 维度 | 行情是公共的（§3.2 铁律），加 session 会让缓存命中率归零。判断标准：**"这份数据在另一个会话里看到，用户会不会觉得不对？"**（§8.6.11.4） |
| D32 | **本轮只用邮箱；短信后置**（🔒 2026-09 已定） | 只做短信 / 现在就接短信 / 邮箱+短信同时上 | **国内短信没有免费路径**：阿里云官方明确"仅企业资质才可以报备和发送"，且**签名实名报备要 5~10 个工作日**（上线阻塞）。邮箱走 SMTP **免费且无准入** → 先上册邮箱，把报备从关键路径移走；短信表结构与通道裁决照建，**将来加 `sms` 只是多一行**（§8.6.8④） |
| D33 | **不允许存在"无任何已验证找回通道"的账号**（本期=邮箱必须已验证） | 允许 | 这种账号忘密码后只能管理员手工重置 —— 把运维成本转嫁给管理员，且用户体验是"这系统没有找回密码"。注册时**邮箱验证通过才落 pending**（§8.6.8④） |
| D34 | **通知额度耗尽必须"明确拒绝"，不能静默失败** | 静默吞掉 | 用户会一直等验证码、反复点"重新发送"，最后投诉"收不到"。返回 `quota_exceeded` + 明示原因（§8.6.8⑥ 纪律 2） |
| D35 | **三种失效分三套处置**：会话到期→静默 refresh；`disabled`→立刻断 WS；`expired`→宽限内只读降级 | 一套逻辑处理全部 | 一套逻辑必出两个坏结果：① 会话到期就把人踢回登录页（每 30 分钟打断一次）；② 账号到期立刻切断（盘中的人被踢，而他只是该续费）。**"到期"是欠费，"禁用"是封号**（§8.6.11.6） |
| D36 | **WS 失效必须"推控制帧 + 用 4xxx close code + 服务端侧停止推送"** | 只关连接 / 统一用 1000 | ① 无控制帧 → 前端只能猜（该重连还是跳登录页）；② 统一 1000 → 前端当成"网络断了"→ **无限重连**，退避也失效；③ **只发帧不切断 = 通知恶意客户端自己停手，无效** —— 服务端必须先停止取数（订阅表按 `session_id` 索引）（§8.6.11.6） |
| D37 | **加 `--env {dev,test,prod}`，生产启动自检不通过就拒绝启动** | 沿用现在的"一个 CLI 无环境概念" | 现状 `MOSS_ENV=test` **只在测试时设**，`cmd_start` 既不设也不校验 → **没有任何东西能判断"我是不是生产"**，§8.7.2 的自检无从落地；调试与生产共用 6.4GB 主库 + 31GB 仓库（§8.7.3） |
| D38 | **零停机的前置条件是 Postgres + Redis，不是反向代理** | 直接上 Blue-Green | SQLite 单写者下**第二个实例根本起不来**（`database is locked`）→ 所以 §10 的 **P3 是零停机的前置条件，不能往后排**；且缓存不外置则新实例 = 冷启动 = **1~3 分钟空数据**（实测首个 `/watchlist` 59.8s、整表重算 200s）（§8.7.4、§8.7.6） |
| D39 | **前端构建原子替换 + 保留旧 hash 资源** | 直接写 `web/dist`（正在被服务） | 构建那几秒用户可能拿到半更新资源；`index.html` 若引用已被删的旧 hash → 用户报"白屏"。**构建到 `dist.new` 再 `os.replace`**，旧资源保留 ≥1 天（§8.7.5） |

---

## 十二、与现有文档的关系

| 文档 | 关系 |
|---|---|
| [`MULTI_TENANCY_DESIGN.md`](MULTI_TENANCY_DESIGN.md) | **仍然有效**：合规原理、四眼原则、审计链、脱敏、部署检查清单以它为准。本文只**扩展**"用户级隔离"与"能力码"，不推翻 |
| [`SECURITY_COMPLIANCE.md`](SECURITY_COMPLIANCE.md) | 数据分级四级制、日志红线；本文 `DataClass` 用法与之一致 |
| [`INTRADAY_T_DESIGN.md`](INTRADAY_T_DESIGN.md) | 做T模块领域设计（口径、因子、坑）；本文 §6.1 是它的多用户化改造清单 |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) / [`RUNTIME_SAFETY_ARCHITECTURE.md`](RUNTIME_SAFETY_ARCHITECTURE.md) | 分层与依赖方向；本文 §8 的进程拆分必须遵守其单向依赖 |
| [`PERFORMANCE_OPTIMIZATION_2026-09-15.md`](PERFORMANCE_OPTIMIZATION_2026-09-15.md) | 已有性能手法；本文 §7 是其**跨用户共享**延伸 |
| [`RESUME_PROJECT.md`](RESUME_PROJECT.md) | 简历与面试叙事；**附录 C 是它的多租户/高并发章节** |
| `docs/PROJECT_AUDIT_2026-09-19.md` | 其中"单进程 + SQLite"被列为已知不足 —— 本文 §8/§10 是解决路径 |

---

## 十三、实现进度（**区分"已落地"与"设计中"**）

> ⚠️ 本文前十二章是**设计**。本章记录**代码里真实存在、且被测试证明**的部分。
> 面试时这两者必须分开讲：把设计说成已完成，第一轮追问就会崩。

状态口径：
- **已落地** = 代码在仓库里 + 有通过的测试 + 本文给出可复核的测试名
- **部分** = 骨架在，但尚未接入业务链路
- **设计中** = 只有本文，没有代码

### 13.1 已完成（P0 的一部分）

| 能力 | 状态 | 落点 | 证明 |
|---|---|---|---|
| 三环境隔离（dev/test/prod） | **已落地** | `src/core/config.py`（`MOSS_ENV` + `assert_environment_consistency`）、`manage.py --env` | `tests/unit/test_env_guard.py` 37 项 |
| 环境变量优先级（7 个路径/密钥字段） | **已落地** | `_env_field` + `populate_by_name` | 同上 9 项（含运行期 `setenv` 回归） |
| 用户/凭证/会话数据模型 | **已落地** | `auth_sqlite_repo.py`：`dim_user` / `dim_user_credential` / `fact_session` / `fact_remember_token` / `fact_verification_code` / `fact_password_reset` / `fact_registration_review` / `dim_user_contact` | `tests/unit/test_auth_service.py` 70 项 |
| 口令强度、锁定、时序侧信道防护 | **已落地** | `src/domain/auth/service.py`（argon2id > bcrypt > pbkdf2；`_dummy_verify`） | 同上 |
| 注册审批流（pending 不可登录） | **已落地** | `fact_registration_review` + `AuthService.register` | 同上 |
| 登录/登出/刷新/改密/找回/会话列表 | **已落地** | `src/api/routes/auth.py` 12 个端点 | `tests/unit/test_auth_routes.py` 23 项 |
| 两层凭证（30 min 滑动 + **12 h 绝对上限**；remember 7 d 轮换 + 重放检测） | **已落地** | `_set_auth_cookies`、`fact_remember_token` | 实测：重开免密 `200`；重放 `401 replay_detected` |
| 通知渠道抽象（邮件/控制台/内存） | **已落地** | `src/infrastructure/notify/` | `test_auth_service.py` |
| **多用户自选池 + 三重配额** | **已落地** | `user_pool_sqlite_repo.py`：`dim_user_pool` / `dim_user_watchlist_v2` | `tests/unit/test_user_pool_isolation.py` 70 项 |
| **个股口径 copy-on-write 三级回退** | **已落地** | 同上：`dim_intraday_profile_v2`（主键含 `user_id`）+ `_template` 表 | 同上（含"读不物化行"与"模板升级能到达未自定义用户"两条） |
| **口径指纹（计算可共享、参数不共享）** | **已落地** | `ProfileRecord.caliber_key`：只由 mode/权重/阈值/档位决定，**不含** user_id、不含板块归属 | 同上 3 项（稳定性 / 区分度 / 与板块无关） |
| 旧口径表迁移脚本（dry-run 默认） | **已落地** | `scripts/migrate_intraday_profile_to_v2.py` | 实测：dry-run 零写入、`--apply` 拆 intraday/daily 两行、幂等、旧表零改动 |
| 老库补列（`ALTER TABLE` 升级路径） | **已落地** | `_ADDABLE` / `_add_missing_columns` | `test_ensure_schema_adds_missing_columns_to_old_database` |
| 标的代码严格校验（6 位 ASCII、拒北交所） | **已落地** | `normalize_code` + `PoolValidationError` | 同上 12 项参数化 |
| 批量加自选（单事务，38× 提速） | **已落地** | `add_stocks_bulk` / `a_add_stocks_bulk` | 同上 7 项，含"批量不慢于逐条"的实测断言 |
| **写操作幂等键**（重试安全） | **已落地** | `src/core/idempotency.py` + `admin.py` 的 `_idempotent` | `tests/unit/test_idempotency.py` 25 项 + `test_admin_routes.py` 4 项 + `scripts/_verify_idempotency_e2e.py`（真实 HTTP 9 项） |
| **存活/就绪探针拆分** | **已落地** | `/api/v1/health/live`（0 I/O、免鉴权）vs `/api/v1/health`（秒级、需登录） | `test_health_performance.py` 3 项（含"探针里不许出现任何 I/O 依赖"的源码级断言） |
| **长连接竞态修复** | **已落地** | `manage.py`：uvicorn `--timeout-keep-alive 65` | `_verify_idempotency_e2e.py` 第 5 项（空闲 6 秒后复用连接） |
| 前端连接状态条 + 网络错误归因 | **已落地** | `web/src/components/ServerStatusBanner.tsx`、`api.ts` 的 `pingServer` / `networkErrorMessage` | 连不上时才出现；区分"服务没跑"与"连接被中断" |
| **定价移出权限配置** | **已落地** | 删除 `TierPlan.monthly_price`（模型 / API / 前端类型 / `configs/platform_tiers.json`）；每项功能的加购价 `pricing` 保留 | `test_platform_config.py`（含"旧前端仍发该字段时忽略而非报错"）、`test_admin_platform_api.py` 3 项 |
| **告警扫描盘中化 + 服务启动即扫** | **已落地** | `registry.py` 三个 `event_alert_scan` 作业：`30 9,10,13,14 * * 1-5`（开盘/10:30/13:30/14:30）、`0 12 * * 1-5`（午盘）、`30 17 * * 1-5`（收盘后全量）；`main._run_event_alert_on_startup`（延迟 90 s、失败只记日志、复用同一把扫描锁）；时刻表由 `alert_scan_schedule()` 从 cron 现算，`/alerts/settings` 与前端共用 | `tests/unit/test_scheduler.py` 6 项（三个 cron / 时刻表展开 / 周末与点位不触发 / 启动补扫三条路径） |

### 13.2 部分完成 / 尚未接线（**如实列清**）

| 能力 | 状态 | 缺口 |
|---|---|---|
| `Principal` 带 `session_id` | **部分** | 字段与 `session_audit_fields()` 已在 `src/core/tenancy.py`；**尚未**把 `session_id` 写进每个审计事件 |
| 配额与套餐联动 | **部分** | `PoolQuota` 可穷举单测；**尚未**从 `dim_user`/角色读套餐自动注入（调用方现在显式传 `quota=`） |
| 自选池从 YAML 迁到 DB | **部分** | 库表与 API 能力已备；`configs/intraday.yaml` 仍是运行时唯一数据源，**尚未**切换 |
| 做T模块缓存加用户/口径维度 | **设计中** | §7.3 的 P1，未动 |
| 活跃宇宙调度 + 盘前预热 | **设计中** | §7.2.5 的 P2，未动 |
| Postgres + Redis | **设计中** | §10 的 P3，未动。**在此之前不要起第二个实例**（SQLite 单写者会 `database is locked`） |

### 13.3 本轮实测中发现并修掉的三个真问题

这三个都不是"设计没想清楚"，而是**写了代码、跑了数据才暴露**的 —— 面试时讲这三个比讲架构图更能证明动手能力：

| # | 问题 | 为什么危险 | 修法 |
|---|---|---|---|
| 1 | `MOSS_ENV=prod` 被静默忽略，环境仍是 `dev` | `env` 是 pydantic-settings 的**保留字段名**，赋值被丢弃 → **整套生产自检静默空转**，而日志上看不出任何异常 | 加 `validation_alias=AliasChoices("MOSS_ENV","env")` |
| 2 | `moss_rt` cookie 存的是 `refresh_token` 而不是 `remember_token` | `/auth/refresh` **恒定 401** → "关掉浏览器再打开免密"这个功能整体不可用 | 修正 `_set_auth_cookies`；实测重开 `200`、重放 `401` |
| 3 | 失败响应返回 `HTTP 200` + `{"ok": false}` | 前端只判状态码 → **把失败当成功**（改密失败却显示"已保存"） | `_to_response` 失败时抛 `HTTPException` |

另有两条**代码评审级别的陷阱**在自选池实现里被主动避开：

| 陷阱 | 后果 | 处理 |
|---|---|---|
| 用 `zfill(6)` 兜底短代码 | `"519"` → `"000519"`：**一只毫无关系的票**，且加得进、取得到数、不报错 | `normalize_code` 要求**恰好 6 位 ASCII 数字**；`"000519"` 这类深市补零写法本身合法，照收 |
| 用 `str.isdigit()` 判合法性 | 全角 `"８３０７９９"` 的 `isdigit()` 为 True 而 `startswith("8")` 为 False → **北交所校验被绕过** | 逐字符判 ASCII，而非 `isdigit()` |

### 13.4 本轮实测数字（`scripts/_probe_pool_latency.py`、`_probe_pool_hotpath.py`）

自选池的读写延迟，**本机实测**（i5-12600K / SATA SSD / SQLite）：

| 操作 | 中位延迟 | 备注 |
|---|---|---|
| `ensure_schema`（已建过表） | 1.4 ms | 只在启动时一次 |
| `sqlite3.connect` + `close` | **0.10 ms** | 连接**不是**瓶颈 |
| `add_stock`（单条，含三重配额校验） | **10.1 ms** | 其中 **7.6 ms 是那条 INSERT 的 fsync** |
| `add_stocks_bulk`（50 只，单事务） | **14 ms** | 逐条循环同样 50 只是 **542 ms** → **38×** |
| `list_stocks`（100 只全量） | 1.4 ms | 读路径亚毫秒级 |
| `distinct_codes`（配额去重统计） | 1.3 ms | |
| `effective_profile`（命中用户档案） | 0.6 ms | copy-on-write 写入后 |
| `effective_profile`（两级回退都未命中） | 1.7 ms | 查了用户档案 + 租户模板两次 |

**三条可以拿去面试的结论**：

1. **瓶颈是 fsync，不是查询**：连接开销 0.10 ms、三条配额 SELECT 合计 2.5 ms，
   而一条 INSERT 要 7.6 ms —— 因为 SQLite 默认每次提交都 fsync。
   所以"'一键全部加自选'很慢"的根因**不在校验逻辑**，在 N 次独立提交。
   收进一个事务后 50 只票 542 ms → 14 ms（**38×**）。
2. **读路径完全不用担心**：1.4 ms / 100 只。真正的瓶颈是**取数**（§7 已实测：
   单实例 5~6 req/s），不是本地库。
3. **copy-on-write 的两级回退只多花 1 ms**：换来的是"系统默认值能升级"
   与"不会膨胀出 4 万行空档案"。这个代价换得值。

> 注意：这些是**本机单进程 SQLite** 的数字。迁到 Postgres 后 fsync 语义不同，
> 但"少提交次数"这条结论不变。

### 13.5 本轮最严重的一个 bug：**测试一直在读写生产库**

> 重要性高于前面三个。它不属于"多租户设计"本身，而是在实现多租户时
> 被顺手发现、且**已经真实污染过生产数据**的一类问题 ——
> 面试时讲这个，比讲架构图更能证明"我会验证、也会怀疑自己的测试"。

#### 现象

`tests/unit/test_auth_routes.py` 的 6 个用例在全量 pytest 里**偶发**失败：
`登录 401`、`UNIQUE constraint failed: dim_user.username`、`接口莫名 429`；
而**单独跑这个文件 100% 通过**。

#### 两次误判与纠正

第一反应是"测试间状态污染 / 顺序依赖"。为此做了两件事：

1. 写脚本二分定位**污染源模块** —— 171 个模块逐个组合跑，
   **没有任何单一模块能复现**；
2. 循环跑 14 次试图抓随机种子 —— 抓不到。

两条路都走不通，说明**方向错了**。于是改为给夹具加一条"隔离哨兵"
（断言装配出来的库路径 == 本用例的临时库）—— 哨兵**当场就炸了**：

```
认证服务没有指向隔离库：期望 ...\test_captcha_issues_token0\route.db
                        实际 data/moss_finagent.db     ← 生产库
```

#### 根因（一行代码）

```python
# src/core/config.py（错误写法）
sqlite_path: str = os.environ.get("MOSS_SQLITE_PATH", "data/moss_finagent.db")
```

`os.environ.get(...)` 在**模块导入时**求值，成为**字段默认值**；而 pydantic
对"有默认值且无 `validation_alias`"的字段**根本不去环境里找**。于是：

| 场景 | 行为 |
|---|---|
| 脚本 / `python -c`：先设环境变量再导入 | ✅ 正常（**这正是它一直没被发现的原因**） |
| **先导入、后 `monkeypatch.setenv`（pytest 夹具）** | ❌ **环境变量被完全忽略** |

夹具以为自己在用临时库，`clear_all()` 实际一直在清**生产库**的认证表。

#### 为什么极难发现

- 报错信息完全不指向根因（401 / "用户名已存在" / 429 都是**下游症状**）；
- 单独跑全绿、全量偶发失败 —— 正是"测试污染"的样子，方向被带偏；
- 同一段代码在脚本里怎么试都对 —— 因为脚本恰好是"导入前设环境变量"。

#### 修复

统一改用 `_env_field(ENV_NAME, default)`，并给 `Settings` 开
`populate_by_name=True`。四种声明方式的**对照实测**：

| 写法 | 读环境变量 | 支持 `Settings(sqlite_path=...)` |
|---|---|---|
| `os.environ.get(...)` 当默认值 | ❌ | ✅ |
| `validation_alias=AliasChoices("MOSS_X")` | ✅ | ❌ |
| 再加小写字段名 | ✅ | ✅ |
| **再加小写字段名 + `populate_by_name=True`** | ✅ | ✅ |

同一陷阱影响 **7 个字段**（`sqlite_path` / `local_quote_dir` / `tushare_token` /
`deepseek_api_key` / `alert_smtp_user` / `alert_smtp_auth_code` / `alert_keywords`），
全部改为 `_env_field`。

#### 防复发

1. **夹具里的"隔离哨兵"**：装配出的库路径 ≠ 临时库就**立刻大声失败**，
   并直接指出"这会读写真实库" —— 把下游症状还原成根因。
2. **参数化回归测试**（`test_env_guard.py`）：7 个字段逐个用**运行期
   `monkeypatch.setenv`** 验证生效；另有反向约束，防止有人删掉
   `populate_by_name` 而破坏按字段名构造。
3. 修复后实测：跑完 231 个认证/池/环境测试后，生产库 `dim_user` 行数
   **仍为 0**（此前每跑一次就多一个测试账号）。

### 13.6 面试讲法（30 秒版）

> 这套东西原来是**单用户单进程**的：参数在 YAML 里、评分缓存没有用户维度。
> 做成多人用，核心不是"加个登录"，而是**分清什么能共享、什么不能**。
>
> 我按三级分：行情是全局的，套餐和模板是租户的，权重、阈值、自选池是**每个用户各自的**。
> 一句话规则是——**计算可以共享，结论可以共享，参数不能共享**。
>
> 落地上最容易错的是个股档案：原来那张表主键就是股票代码，**结构上存不下两份**，
> 所以换了张主键含 `user_id` 的新表，并且**只有用户真改过才落库**（copy-on-write）——
> 读的时候不物化，否则系统默认值以后就再也升不动了。
>
> 缓存键我用的是**口径指纹**而不是 user_id：两个人参数一样就共用一次计算，
> 参数不同自然分开，既不重复算也不会串。
>
> 隔离我是用测试钉死的，不是靠"我记得加了 where 条件"——包括
> "知道对方 pool_id 也读不到"这种越权路径。

### 13.7 一个"看起来像前端 bug"的后端连接竞态（2026-09-23 实测）

这一节单独写，因为它的**排查成本远高于修复成本**，而且症状具有极强的
误导性 —— 面试时讲它，比讲任何架构图都更能证明"能把问题追到底"。

#### 现象

管理员在控制台点「用户管理 → 直接开号 → 创建」，浏览器只弹一句：

```
Failed to fetch
```

而服务端日志里**根本没有这条请求** —— 也就是说请求在到达应用之前就消失了。
"日志里没有"这个事实，把人直接推向"后端没启动 / 端口不对 / 前端配错了
baseURL"，于是反复重启服务、检查端口、重新构建前端，全部无效。
用户侧的表现是"反复点，偶尔成功一次"。

#### 排除过程（每一步都要有**可证伪**的依据，不能靠猜）

| 假设 | 怎么验证 | 结论 |
|---|---|---|
| 后端没运行 | `manage.py status` + 直接 HTTP 打 `/api/v1/health` | **否**，服务健康 |
| 前端 API baseURL 写错 | 读 `api.ts`：全部是相对路径 `/api/v1/...` | **否** |
| 请求体被 FastAPI 拒（422） | 服务端日志里连 422 都没有 | **否**，请求没到 |
| 密码哈希阻塞事件循环（1~3s） | 写探针量：`hash_password` 144 ms；开号端到端 1.0~1.2 s | **否**，且不阻塞 |
| CORS 拦截 | 同源部署，且失败请求**没有任何**服务端记录 | **否** |
| **连接在到达应用前被关掉** | 对照 uvicorn 启动参数：默认 `--timeout-keep-alive 5` | **是** |

#### 根因

uvicorn 默认**空闲 5 秒就主动关闭 keep-alive 连接**，而浏览器的空闲连接
复用窗口要长得多（60~300 秒）。两边计时器交错时会出现：

```
t+5.00s  服务端：这条连接空闲超时，关闭（发送 FIN）
t+5.01s  浏览器：把 POST /api/v1/admin/users 写到这条刚被关闭的 socket 上
t+5.02s  浏览器：ERR_EMPTY_RESPONSE → TypeError: Failed to fetch
```

为什么**只有**管理后台那些按钮中招，而"刷页面"看起来一切正常：

- **GET/HEAD 会被浏览器静默重试**（幂等，重试无副作用）→ 故障被自动掩盖；
- **POST/PATCH/PUT 不会**（非幂等，浏览器不敢替你重发）→ 只有写操作暴露。

这条规律解释了用户反馈里最费解的一点："页面能刷出来，就是点按钮报错。"

#### 修法（分三层，缺一层都不完整）

| 层 | 改动 | 作用 |
|---|---|---|
| 根因 | `manage.py` 的 uvicorn 参数加 `--timeout-keep-alive 65` | 让**服务端**的连接在浏览器放弃它之前一直有效，竞态窗口消失 |
| 兜底 | `api.ts` 对网络层 `TypeError` 自动重试一次（幂等方法，或调用方声明 `retrySafe` 的写操作） | 覆盖后端重启/休眠唤醒后**已经存在的**半开连接 |
| 可观测 | 新增 `GET /api/v1/health/live`（0 I/O）+ 前端顶部连接状态条 | 下次真出问题，用户一眼看到"服务不可达"，而不是从某个按钮的报错里猜 |

#### 重试带来的新问题：写操作可能重复执行

"自动重试"和"服务端其实已经执行成功、只是响应丢在回程"这两件事，
**客户端无法区分**。对开号接口而言，重试就等于**开出两个账号**。

所以按 Stripe / AWS 的标准做法加了 `X-Idempotency-Key`（`src/core/idempotency.py`）：

- 键由前端对一次"用户意图"生成（`crypto.randomUUID()`），重试时**复用同一个键**；
- 服务端见过这个键就**回放上次响应**，不重新执行；
- 键按 `scope:principal:client_key` 再命名一次空间 —— 响应体里含**初始密码**，
  不按主体做命名空间，猜到别人的键就能读到别人的密码；
- 失败路径**必须** `release` 占位，否则一次失败的请求会把该键锁在"执行中"，
  用户之后每次重试都卡在等待里（**比重复执行更糟，且完全不可见**）。

内存实现，单进程足够；多 worker 必须换 Redis —— 这条限制写在 `stats()` 的
返回值里，不只写在文档里（会腐化的注释不如一个能被断言的字段）。

#### 顺带修掉的第二处：探针不能拿"就绪"当"存活"

给前端做连接状态条时，第一版直接复用了 `/api/v1/health`。这是**错的** ——
本项目自己的测试文件就记录着它曾经 **300 秒超时**、以及 142.8 s / 144.5 s
的排队实测（原因：它要连 Ollama、校验 LLM 审计链、聚合 14 GB 库的统计）。
拿它当存活探针，服务只是"忙"就会被判成"死"，前端会对着一个正常的后端
弹出"无法连接服务器" —— **误报比不报更伤信任**。

所以按标准拆开：

| 路径 | 语义 | 成本 | 鉴权 | 谁用 |
|---|---|---|---|---|
| `/api/v1/health/live` | 进程活着吗 | **0 I/O**（实测 <1 ms） | **免鉴权** | 前端状态条、网络错误归因 |
| `/api/v1/health` | 依赖都好吗 | 秒级（实测 456 ms~300 s） | 需登录 | 运维/诊断页 |

两个细节是刻意设计的，都写进了测试：

1. **存活探针必须免鉴权** —— 它在**登录页**就要能用（那时还没有会话），
   而且 k8s liveness probe 本来就不该带凭证；
2. **它不能返回 pid / 环境名 / 版本号** —— 匿名可读的接口不该泄露
   部署信息（`dev` 这个环境名本身就是提示）；
3. **聚合健康度 `/api/v1/health` 故意不放免鉴权清单** ——
   它会返回模型配置状态、数据源健康度、库表行数，那是内部拓扑。

#### 这一节的面试讲法

> 有个 bug 的表现是"点创建按钮报 `Failed to fetch`，但服务端日志里
> 完全没有这条请求"。日志里没有，说明请求没到应用 —— 这排除了所有
> 业务逻辑问题，指向传输层。
>
> 真正的原因是 uvicorn 默认 5 秒关空闲连接、浏览器复用窗口更长，
> 两者计时器交错。而它**只影响写操作**：GET 会被浏览器静默重试，
> POST 不会。所以"页面能刷、按钮报错"。
>
> 我没有只加个重试就算了 —— 重试会让"响应丢了"变成"开了两个号"，
> 所以同时给写接口加了幂等键，让服务端来判定"这个请求我见过没有"。
> 另外做连接状态条的时候我一开始用了 `/health`，然后发现这个项目
> 自己的测试里记着它 300 秒超时过 —— 拿它当存活探针会把"忙"报成"死"，
> 就拆成了 `/health/live`（0 I/O）和 `/health`（就绪）。

### 13.8 从"dev 直接对外"到"对外试点实例"：三个必须一起解决的问题

起因是一个很实际的问题：**客户在外网，怎么让他们登录？**
最省事的做法是"把 dev 实例开出去"。这一节记录为什么不行、
以及顺着这个问题挖出来的三个真问题。

#### 问题一：dev 实例的匿名面比想象的大得多（实测）

在 dev 实例上**不带任何 Cookie** 直接打接口，实测结果：

| 路径 | 未登录结果 | 后果 |
|---|---|---|
| `/api/v1/intraday/watchlist` | **200** | 整份自选清单 |
| `/api/v1/intraday/snapshot?code=600036` | **200** | 整套做T取数/计算入口 |
| `/api/v1/scheduler/jobs` | **200** | 内部任务配置 |
| `/api/v1/metrics` | **200** | 运行指标 |
| `/docs`、`/openapi.json` | **200** | 完整接口面（路径/参数/schema） |
| `/api/v1/auth/_debug/whoami` | **200** | **库路径 + 用户名列表** |
| `/api/v1/admin/*`、`/api/v1/me/*` | 401 ✅ | 这些路由自己查了会话 |

根因：`TenancyMiddleware` 在 `MOSS_TENANCY_ENFORCE` 未打开时，会把
没有凭证的请求当成"本地开发身份"（`_LOCAL_DEV`）放行，而它的凭证解析
只认 `Authorization: Bearer` 与开发用请求头 —— **不认本项目的会话 Cookie**。
于是"每个路由自己查会话"成了唯一防线，而只做了一半的接口就裸着。

**结论：dev 实例绝不可对外。** 这不是"稳妥起见"，上表里任何一行
放到公网上都是一个事故。

#### 问题二：`manage.py --env prod` 的自检**从来没跑过**（最严重的一个）

为了做对外实例，先给启动了"失败即拒绝启动"的门槛。然后实测发现：

```
父 shell 里没有 MOSS_ENV
  → Settings().env == "dev"        # ← 用**父进程环境**构造的
  → settings.is_prod / is_pilot 全为 False
  → 目标环境的那组规则**整段不执行**
  → problems == []
  → 打印"自检通过"
```

也就是说 `manage.py start --env prod`（以及新的 `--env pilot`）
**从来没有执行过生产自检**，而它自己还以为执行了。原有的注释写着
"自检用即将生效的环境变量，而不是当前进程的（否则 `--env` 白给）"——
意图是对的，但只把注入后的环境当成 `environ=` 参数传了进去，
`Settings` 本身仍从 `os.environ` 构造，两边看的不是同一份配置。

这正是自检本来要防的那类事故（"以为连的是测试、实际连的是生产"），
只不过这次失效的是**自检自己**，而且**没有任何迹象**。

修法：构造 `Settings` 前用 `_temporary_environ` 把注入值真的写进
`os.environ`（退出时逐键还原，区分"原本没有"与"原本有"）。
修好之后的对比很直观：

```
修复前： manage.py start --env prod  →  ✅ 自检通过（实际一条没查）
修复后： manage.py start --env prod  →  ❌ 拒绝启动：
   - 生产必须配置真实邮件通道（ALERT_SMTP_USER + ALERT_SMTP_AUTH_CODE）……
   - 生产必须用 postgres（当前 'sqlite'）……
   - 生产 DB 不应指向 localhost……
   - 生产必须设 MOSS_TENANCY_ENFORCE=1……
   - 生产必须配置 MOSS_IDENTITY_SECRET……
```

回归测试直接钉住这件事：把 pilot 预设里"承认单实例"那一项抽掉，
自检**必须**报出来（`test_manage_env_selfcheck_validates_the_target_environment`）。

#### 问题三：pilot 这颗"第四档"必须与 dev 有**结构性**差别

只加一个 `--env pilot` 把路径改掉，等于开了个端口号不同、安全等级相同的
dev。所以门槛写成**共用同一段代码**，只放宽"必须 Postgres"这一条：

| 门槛 | dev | pilot | prod |
|---|---|---|---|
| 数据目录必须独立 | `data/dev/` | **`data/pilot/`（路径含 pilot，否则拒绝启动）** | Postgres |
| 真实邮件通道 | 可选 | **强制** | **强制** |
| 禁请求头身份 | — | **强制** | **强制** |
| 禁 console 通知通道 | — | **强制** | **强制** |
| 登录门槛 | 关 | **自动强制**（`is_public`） | 自动强制 |
| 交互式文档 | 开 | **不注册** | 不注册 |
| Cookie `Secure` | 关 | **开** | 开 |
| 单实例告知 | — | **必须显式 ACK** | — |
| Postgres | — | 不支持（后端未实现，报错说清） | **强制** |
| 多租户 `TENANCY_ENFORCE` | — | **禁止打开**（见下） | **强制** |

最后一行看起来反直觉，但它是**当前代码的真实约束**：
`resolve_principal` 只认 Bearer 令牌，不认会话 Cookie，而前端从不发
`Authorization`。所以打开 `MOSS_TENANCY_ENFORCE=1` 会让**每个浏览器请求**
都 401 —— 连 `/`（登录页本身）都打不开，症状是"整站白屏 401"，
看上去像前端坏了。自检因此**主动拒绝**在 pilot 上打开它，并把原因写进
错误信息；等"会话→Principal"的桥接落地后，这一条要改成"必须打开"。

#### 顺带补掉的一个真洞：prod 忘了配邮箱 = 验证码进日志

`_is_console_notifier` 原本只看"有没有显式把 `MOSS_NOTIFY_CHANNEL` 设成
console"，而 `build_notifier` **在完全没有 SMTP 凭据**时也会回落到
`ConsoleNotifier`（那是给 dev 的便利）。两者叠加：

> 一个"prod 但忘了配邮箱"的部署能通过自检顺利上线，
> 验证码全部写进 `data/run/backend.log`，而界面上提示"已发送"。

所以公网环境（prod **和** pilot）现在**强制要求**
`ALERT_SMTP_USER` + `ALERT_SMTP_AUTH_CODE`。这条一加，
"先配 SMTP 授权码、再开公网"从**建议**变成了**代码强制**的顺序 ——
没配好时 `manage.py start --env pilot` 直接拒绝启动并说明原因。

#### 落地清单

| 改动 | 落点 | 证明 |
|---|---|---|
| 第四档 `pilot` | `src/core/config.py`：`ENVS` / `is_public` / `is_single_instance` / `describe_environment` | `test_env_guard.py` 53 项 |
| pilot 启动预设（独立库、关调度、单实例 ACK） | `manage.py`：`PILOT_ROOT` / `pilot_isolation_env` | 同上（含 `--env` 自检针对目标环境的回归） |
| 登录门槛（公网自动强制） | `src/api/login_gate.py` + `main.py` 注册 | `test_login_gate.py` 33 项（覆盖实测暴露的每一条路径） |
| 文档/schema 不对外 | `docs_kwargs`（不注册）+ `DOC_PATHS`（动态 404） | 同上 |
| 就绪探针需登录后 `manage.py status` 不误报 | `manage.py`：存活探针兜底签名 + 401 专门文案 | `test_manage_preflight.py` |
| 会话只读校验（不写库） | `AuthService.validate_session`（与 `touch()` 共用同一份有效判据） | `test_login_gate.py` 的停用/登出即刻失效两项 |
| 补 SMTP 强制 | `config.py` 公网门槛 | `test_env_guard.py` 参数化两项 |

#### 这一节的面试讲法

> 客户要在外网登录，最省事的做法是把开发实例开出去。我先去量了一下
> **开发实例到底有多开放**：不带任何 Cookie，量化接口、调度配置、
> 指标、OpenAPI 文档全是 200，连一个诊断端点都会返回库路径和用户名列表。
> 所以这条路直接否掉。
>
> 然后我加了"失败即拒绝启动"的对外档。加完发现一个更值得说的问题：
> 原来 `manage.py --env prod` 的自检**从来没跑过** —— 它用父进程的环境
> 构造配置，而父 shell 里没有 `MOSS_ENV`，于是目标环境的规则一条都没执行，
> 却打印"自检通过"。这正是自检要防的那类事故，只不过这次失效的是自检自己。
> 修完之后 `--env prod` 会一次列出五条缺失项。
>
> 最后是"为什么不能直接把多租户鉴权打开"：那层的凭证解析只认 Bearer 令牌，
> 不认我们自己发的会话 Cookie，而前端从不发 Authorization ——
> 打开就是全站 401，连登录页都打不开。所以先落地"登录门槛"这一半
> （认 Cookie），并在自检里**主动禁止**在对外档打开那另一半，
> 把原因写在错误信息里，而不是留给人踩。

---

### 13.9 调度器里的三个真问题（2026-09-24 实测，由"告警页一条都没有"挖出来）

起因：客户问「事件告警页为什么永远 0 条」。表层原因是扫描班次只有一班
（工作日 17:30），盘中根本没有扫描；顺着查下去，又挖出两个更值得讲的东西。

**① 一个从不被读取的开关：`MOSS_SCHEDULER_ENABLED=0`**

`manage.py` 在 dev / pilot 隔离环境里都写了这个变量，启动 banner 也如实印着
「定时任务：**已关闭**」。但**全仓库没有任何一处读它**（`grep -rn MOSS_SCHEDULER_ENABLED src/`
零命中），`main.py` 的 lifespan 无条件 `CronScheduler(...).start()`。
实测证据：dev 实例的 `data/dev/scheduler/runs.jsonl` 有 101 条运行记录。

- 危害不是"多跑几个作业"，而是**两件事同时发生**：对外宣称"演示实例不占行情源"，
  实际它和主实例在同一份 31 GB `data/quant` 上跑同一批下载作业（`manage.py` 自己
  的注释就警告过"两个调度器同写 data/quant 会重复下载"）；
- 这类 bug 的一般形态值得记：**开关只写了"设置"没写"读取"**，`grep` 变量名就能抓；
  如果当初写了"设置 + 读取"两端的测试，`--env dev` 的 banner 就不会撒谎。
- 本轮**故意不改行为**：用户此刻要的正是"告警扫描必须跑起来"；真要关，得在
  `CronScheduler.start()` 里读该变量，而且必须区分"全关"与"只关行情类作业"
  （告警/竞价这些作业在 dev 有调试价值）。

**② 班次设计：告警不该只在收盘后**

原注册表只有 `event_alert_daily`（`30 17 * * 1-5`）。对做T/盯盘的用户来说，
17:30 出来的风险提示是**复盘**不是**盘面**；而盘中四个信息增量最大的时点
（09:30 隔夜落地、10:30 早盘发酵、12:00 午休消化、13:30/14:30 午后与尾盘）
反而没有扫描。改成三班（`30 9,10,13,14` / `0 12` / `30 17`，均限工作日），
再加"服务一启动就补扫一次"（延迟 90 s，避开首屏与板块预热争抢）。
改完的**时刻表由 `alert_scan_schedule()` 从 cron 现算**再交给前端 —— 避免
"改了 cron 忘了改提示"这类最常漂的文案。

**③ 一个定时炸弹测试：`expire_time` 写死绝对日期**

`tests/unit/test_event_repository.py` 的 `_alert()` 夹具把 `expire_time` 写成
`2026-09-21`。读路径的懒过期（`_expire_due`）会把到期告警置 `expired`，而
`list_alerts` 默认 `status <> 'expired'` —— 于是**日期一过，4 条用例集体转红，
被测代码一行没改**。产品行为（默认隐藏过期）本身是对的，另有 `_alert_expired()`
专门覆盖。修法是让夹具用 `now + 7 天`。

- 讲法：**"绝对时间进夹具"是把测试租给日历**。凡是"未来/过去"的语义，
  夹具里都该用相对时间；断言"已过期"的用例则相反，要能显式构造过去时间。

### 13.10 一次"事件告警永远 0 条"的四层根因（2026-09-24，全部有实测数据）

现象：客户打开「事件告警」页，**永远 0 条**；而调度面板里扫描作业天天 `success`。
顺着往下挖，四层原因叠在一起，**每一层都足以单独造成 0 告警**，而且每一层都"看起来正常"。

| 层 | 根因 | 证据 | 修法 |
|---|---|---|---|
| ① 班次 | 扫描只有一班：工作日 17:30 | 注册表 `event_alert_daily` cron=`30 17 * * 1-5`；`fact_alerts` 0 行而 `fact_events` 356 行 | 三班：`30 9,10,13,14`、`0 12`、`30 17`，加"服务启动补扫一次" |
| ② 候选饥饿 | 日程型事件把 `publish_time` 写成**未来**披露日，按它倒序取候选 → 永远占满窗口 | 87 条已分析事件**来源 100% 是"财报披露日程"**（publish_time 09-28~10-01），184 条当天快讯一条没轮到 | `dedup.event_recency_key`：未来时间收敛到 `fetch_time`，同刻真新闻优先；积压出队改"取一窗 + Python 按该规则排序" |
| ③ 阈值标定 | 门槛（risk 中 60/opp 中 65/conf 0.70）**高于模型实测分布的最大值** | 实测 24 条真快讯：risk max=45、opp max=45、conf max=0.70；"低"档 45 还被 `min_level=medium` 拦掉 | 先给 stage-2 提示词写死 0-100 分档锚点，再按重测分布把中档压到 p75~p90 |
| ④ 静默降级 | `.env` 里 `DEEPSEEK_API_KEY=...` **被粘在注释行尾**，dotenv 整行当注释丢掉 → reasoning 档静默降级到本机 `qwen3:8b` | 审计：`model=deepseek-flash err=config_error: DEEPSEEK_API_KEY未配置` 紧跟 `model=qwen3:8b`；作业仍记 `success` | `scripts/fix_env_glued_assignments.py`（拆行、前后各解析一次并断言"键集合不缩水"）；同一条链上还救回 `QMT_ENABLED=0` / `MOSS_EM_DIRECT=1` |

**④ 最值得讲**：它不是"配置写错"，而是**配置存在、格式合法、却被解析器当注释吃掉**。
`dotenv` 是逐行解析的，赋值只要没独占一行就整行作废；而失败的表现是
**功能静默降级**（换一个更弱的模型继续跑，作业状态还是 success），
唯一线索是审计日志里"先一条 `config_error`、后一条换模型的成功调用"。
这类 bug 的通用检测手段：**把"降级发生了"变成可观测指标**（降级次数、当前生效模型），
而不是只看作业 status。

**测量方法与结果**（`scripts/_probe_alert_scores.py` → `data/run/alert_probe.txt`）：

```
降级到本机 qwen3:8b（n=17）：风险 p50=0  p90=15 max=58｜机会 p50=35 p90=55 max=55｜把握度 p50=0.55 p90=0.68 max=0.72
真 DeepSeek-flash（n=20）： 风险 p50=0  p75=40 p90=52 max=58｜机会 p50=18 p75=38 p90=42 max=50｜把握度 p50=0.66 p90=0.70 max=0.72
```

结论：**换模型带来的分数变化，比调提示词大得多** —— 这也是为什么"先看生效模型、再看提示词、
最后才动阈值"是正确顺序；先调阈值只会把①③两层问题一起掩盖掉。
标定后（conf≥0.60、risk 70/50/35、opp 65/50/35）同一批 20 条事件有 5 条成告警（25%），
其中"沪指失守3900点/创业板指跌逾2%"命中风险中档、"哈药股份跌停"命中 58 分 —— 与人工判断一致。

### 13.11 自定义域名上线：`https://moss.wujiaitool.cn` → 本机 8110（2026-09-24 实测）

对外试点从"随机 trycloudflare 地址"换成**自有域名 + 命名隧道**，客户拿到的链接
从此固定；整条链路与踩到的三个坑记录如下。

**链路**：`wujiaitool.cn`（阿里云注册 + 实名）→ Cloudflare 托管 zone
（NS = `alexandra/giancarlo.ns.cloudflare.com`）→ 命名隧道 `moss-pilot`
（id `6e41d39f-…`）→ 本地 ingress `http://127.0.0.1:8110`（pilot 实例）。

**验证证据**（不是"应该能通"，是实测）：

| 探测 | 结果 |
|---|---|
| `https://moss.wujiaitool.cn/` | **200**，`<title>Moss-FinAgent-Research 投研工作台</title>` |
| `/api/v1/health/live` | 200（免鉴权探针，给 Cloudflare/监控用） |
| `/api/v1/health` | **401** —— 登录门槛在隧道后面照样生效 |
| `cloudflared` 日志 | 4 条 `Registered tunnel connection`（sjc10/lax09/sjc08/lax10，QUIC），pre-check 全 PASS |

**坑 1：`cloudflared tunnel login` 的 OAuth 回调在本机取不回证书**（两次复现）。
浏览器里授权成功，CLI 却一直 `Waiting for login...`，第二次报
`Failed to fetch resource` / `Failed to write the certificate`。**绕法**：
让浏览器把证书**下载**下来（授权页有这条兜底路径），再手工放到
`%USERPROFILE%\.cloudflared\cert.pem` —— 注意它长得不像普通 PEM：
内容是 `-----BEGIN ARGO TUNNEL TOKEN-----` + base64 的 `{zoneID, accountID, apiToken}`，
用 `cloudflared tunnel list` 能列出（空而已）就说明认了。

**坑 2：负缓存的假故障。** 隧道建好、权威 NS 能查到 A 记录，但本机
`Resolve-DnsName` 仍报 `DNS name does not exist` —— 那是**解析器把之前的
NXDOMAIN 缓存住了**（223.5.5.5 当时还是旧答案，8.8.8.8 / 1.1.1.1 / 119.29
都已返回正确 A）。用 `ipconfig /flushdns` 只清本机、清不掉上游；
判断"隧道到底通没通"要**绕开 DNS**：

```powershell
curl.exe -s -o NUL -w "%{http_code}" --resolve moss.wujiaitool.cn:443:104.21.47.185 `
  https://moss.wujiaitool.cn/
```

**坑 3：前台跑的隧道会随终端/会话消失。** 已注册登录自启计划任务
`MossCloudflaredTunnel`（`cloudflared tunnel --no-autoupdate run moss-pilot`，
失败自动重启 3 次，不随电池/空闲停止）—— 客户访问不该依赖某个终端窗口还开着。
（想更彻底可 `cloudflared service install` 装成系统服务，需管理员权限。）

**仍待办**：**Cloudflare Access 邮箱白名单**。现在没配 Access，所以
"知道 URL 的人都能看到登录页并尝试撞库/注册"；配上之后没在白名单里的访问者
**在边缘就被挡掉，连登录页都看不到**（`scripts/cloudflare_setup.py --access`
可用 API 一次性建掉，也可以面板点：Zero Trust → Access → Applications →
Self-hosted，域名填 `moss.wujiaitool.cn`，Policy = Allow + Emails）。

## 附录 A · 权限码 × 角色 默认矩阵（提议稿）

`●` 默认允许　`○` 需显式授权（四眼）　`—` 默认拒绝

| 能力码 | researcher | pm | trader | risk | compliance | auditor | admin |
|---|:--:|:--:|:--:|:--:|:--:|:--:|:--:|
| `research.analyze.run` | ● | ● | — | ● | — | — | — |
| `research.read.own` | ● | ● | — | ● | ● | ● | — |
| `research.read.tenant` | — | ● | — | ● | ● | ● | — |
| `research.export.data` | ○ | ○ | — | ○ | ○ | — | — |
| `quant.intraday.read` | ● | ● | ● | ● | ● | ● | ● |
| `quant.intraday.watchlist.write` | ● | ● | ● | — | — | — | — |
| `quant.intraday.profile.write` | ● | ● | ● | — | — | — | — |
| `quant.intraday.level_fit.run` | ● | ● | ● | — | — | — | — |
| `quant.selector.read` | ● | ● | ● | ● | ● | ● | ● |
| `quant.selector.run` | ● | ● | — | — | — | — | ○ |
| `quant.selector.train` | — | — | — | — | — | — | ○ |
| `quant.auction.read` | ● | ● | ● | ● | ● | ● | ● |
| `quant.auction.watch.write` | ● | ● | ● | — | — | — | — |
| `mainline.read` | ● | ● | ● | ● | ● | ● | ● |
| `mainline.backtest.run` | ● | ● | — | ● | — | — | — |
| `mainline.data.sync` | — | — | — | — | — | — | ○ |
| `fundflow.read` | ● | ● | ● | ● | ● | ● | ● |
| `fundflow.watch.write` | ● | ● | ● | — | — | — | — |
| `crowding.read` | ● | ● | ● | ● | ● | ● | ● |
| `crowding.config.write` | ● | ● | ● | — | — | — | — |
| `crowding.refresh.run` | ● | ● | — | ● | — | — | ○ |
| `alerts.read` | ● | ● | ● | ● | ● | ● | ● |
| `alerts.settings.write` | ● | ● | ● | ● | — | — | — |
| `alerts.scan.run` | ● | ● | — | ● | — | — | — |
| `scheduler.read` | — | — | — | ● | ● | ● | ● |
| `scheduler.job.run` | — | — | — | — | — | — | ○ |
| `metrics.read` | — | — | — | ● | ● | ● | ● |
| `platform.user.manage` | — | — | — | — | — | — | ○ |
| `platform.source.manage` | ○ | — | — | — | — | — | ○ |
| `platform.audit.read` | — | — | — | ● | ● | ● | — |
| `compliance.symbol_list.read` | — | — | — | ● | ● | ● | — |

> 两处刻意的"默认拒绝"，都有依据：
> ① `platform.audit.read` 对 admin 拒绝 —— 审计与系统管理分离（NIST AC-5）；
> ② `compliance.symbol_list.read` 不含 admin —— 名单"高度保密"（指引第十九~二十二条），
>    admin 属"平台"墙，本就不该看合规名单。

## 附录 B · 现有代码接入点速查

| 要改的东西 | 文件:行 |
|---|---|
| 身份派生 / 强制鉴权开关 | `src/api/tenancy_middleware.py:134,204,223` |
| 令牌校验（加过期/吊销/控制台受众） | `src/api/tenancy_middleware.py:134 principal_from_token()` |
| 授权单一入口 | `src/core/policy.py:160 authorize()` |
| 角色 / 数据分级 / 隔离墙 | `src/core/tenancy.py:37,49,69,92` |
| 缓存键隔离 | `src/core/tenancy.py:208 tenant_cache_key()`（**生产代码零调用**） |
| 系统上下文（后台任务降权） | `src/core/tenancy.py:198 system_scope()`（**生产代码零调用**） |
| 行级守卫登记表 | `src/infrastructure/security/rls.py:38,50` |
| RLS DDL 生成 | `src/infrastructure/security/rls.py:156` |
| 做T服务（30 个缓存字段） | `src/intraday/service.py:246-343`（`__init__` 段） |
| 做T自选写盘 | `src/intraday/config.py:1143 save_watchlist()` |
| WS 每连接各算 | `src/api/routes/intraday.py:396` |
| 信号推送（隐私红线） | `src/intraday/service.py:1155 _push()` |
| 权重档案表结构 | `src/infrastructure/repositories/intraday_profile_sqlite_repo.py:37` |
| 仓储工厂（加 Postgres 实现） | `src/infrastructure/repositories/repository_factory.py:58` |
| 进程启动装配（按角色裁剪） | `src/api/main.py:214 lifespan()` |
| 定时任务注册表 | `src/scheduler/registry.py` |
| QMT 进程级锁（多进程的坑） | `src/core/qmt_guard.py:52` |
| 前端页签与模块容器 | `web/src/App.tsx:161`、`web/src/components/QuantTabContainer.tsx` |
| 前端 API 客户端（加 token/错误处理） | `web/src/api.ts` |
| 压测脚本（并发能力，改造前后对比） | `scripts/_probe_concurrency.py` |
| 数据源容量探针（接口级实测） | `scripts/_probe_datasource.py` |
| 批量快照上限探针（300~500 只/请求的依据） | `scripts/_probe_quote_batch.py` |

---

## 附录 C · 面试防御包（投「量化开发」与「AI 应用开发」双岗）

> **为什么这一节存在**：[`RESUME_PROJECT.md`](RESUME_PROJECT.md) §D 有一条自我约束——
> "❌ 不要写**高并发 / 分布式** —— demo 阶段是单进程 SQLite，写分布式会被追问到崩"。
>
> ⚠️ **这条约束只约束"简历 / 自我介绍"，不约束"设计 / 面试展开"**。
> 两者纪律不同，别互相污染：
>
> | | 简历 / 自我介绍 | 架构设计 / 面试展开 |
> |---|---|---|
> | 回答的问题 | 我**做到了**什么 | 我**判断**该怎么做 |
> | 证据要求 | 代码 / 测试 / 实测 | **推演、取舍与失效分析** |
> | 上限 | 只能写到已验证的边界 | **可以放开写**，越深越好 |
>
> 所以本文正文里的多副本、分片、一致性等级、故障矩阵（§8.5）
> **都是可以讲的设计**，而且**讲得越深越好**；违规的只有一种：
> **把设计说成已实现**。
>
> **引用纪律（只有这一条是硬的）**：
> `实测` = 本仓库可复现；`目标` = 设计值；`设计推演` = 未验证的分析。
> **三者不许混说。**

### C.1 核心叙事：把"性能事故"讲成"工程判断"（STAR）

面试官对"我做了个多租户平台"没兴趣，对**"你怎么知道瓶颈在哪"**极有兴趣。
这个项目最好的故事不是"我实现了"，而是"**我原本以为要加实例，实测后否掉了自己**"。

| 环节 | 内容 | 可引用的证据 |
|---|---|---|
| **S 情境** | 单用户桌面版要变成 100 人远程共享的量化平台。第一反应是"多开几个实例横向扩"。 | —— |
| **T 任务** | 在动手之前先确认：**瓶颈到底是不是单实例容量**？ | —— |
| **A 动作** | 写探针压测（`scripts/_probe_concurrency.py`，纯 stdlib、只读接口、不写配置）逐级加并发，同时读整机 CPU。 | 脚本可复跑；结果表见 §2.2 H3 |
| **R 结果 ①（`实测`）** | 单实例吞吐**饱和在 5~6 请求/秒**；同一只票并发 5 的 p50 是 0.95s ≈ **5× 单发** → 说明**零共享**；自选列表（走缓存）并发 60 仍有 **245/s**。 | 同上 |
| **R 结果 ②（关键判断）** | 压测期间**整机 CPU 只有 6~13%**。所以瓶颈**不是核数**，是"1 条事件循环 × 0.3s 同步 pandas"。**在这个前提下加副本，只会把同一份数据源请求放大 N 倍**，而且 §8.1 的 QMT 进程级锁根本不允许。 | CPU 采样 + `qmt_guard.py:52` |
| **R 结果 ③（设计结论）** | 先修"零共享"与"计算在事件循环里"，而不是加实例；并给出可验证目标：计算次数 **6.7 次/s → 0.5 次/s**（按 `(code, 口径指纹)` 去重）。 | §7.1 |

**这个故事的杀伤力在于**：它同时展示了
① **不预设结论、先测量**；② **能区分瓶颈类型**（核数 vs 事件循环 vs 重复计算）；
③ **敢于否掉自己最初的方案**。这三点比"我搭了个分布式系统"更难伪装。

> **金句（与 `INTERVIEW_PLAYBOOK.md` 第 1139 行的既有金句一脉相承）**：
> *"并发降的是单轮成本，缓存降的是重复次数。这次我把两个维度都量了一遍——
> 发现我的问题是**重复次数**：100 个人看同一只票，我算了 100 遍。"*

### C.2 十个尖锐追问与应答（备好，别临场编）

| # | 追问 | 应答要点（都能指向本文某节） |
|---|---|---|
| 1 | 你这套是**真分布式**吗？ | **不是，也不打算现在就是**。实测瓶颈是重复计算不是容量，所以先做共享层（§7.1），再按**资源特征**拆进程（§8.2），最后才是副本。**能力边界写清楚了**：阶段 A 单副本单 worker，60 人以内（`设计推演`）。 |
| 2 | 为什么**不**用 schema-per-tenant？ | 100 用户 / 10 租户 / 个人数据 < 1 万行。有规模判据（Citus：5~50 租户可用独立库、上千租户须共享表），但**数据分层后结论反转**：31GB 行情必须共享，私有数据独立与否无所谓。（ADR D1） |
| 3 | 为什么不直接上 **Zanzibar/OpenFGA**？ | 资源层级固定三级、无"用户组嵌套共享"需求；引入外部授权服务 = 多一个必须高可用的组件 + 一致性窗口。但我**借用了它的两个结论**：撤权延迟用权限版本号（zookie 思想）、共享档位用 relation 化三档。（ADR D2） |
| 4 | **RLS 是不是安全边界**？ | **不是**。它防"忘了写 `WHERE`"，防不住拿到连接的裸 SQL 与运维直连。真正边界是**数据库策略 + 网络 mTLS**。而且 RLS 有官方文档记载的四个坑：`FORCE` 不能省、owner/superuser 绕过、**参照完整性检查绕过 RLS 形成隐蔽信道**、连接池复用串号（§2.5、§5.4）。 |
| 5 | 用户级隔离为什么和租户级分开？ | 因为**它们是正交的两档**：租户 = 部门（部门内共享合理），用户 = 个人（自选池/权重必须私有）。混成一个维度会导致"要么全共享要么全隔离"。（§3、§4.1） |
| 6 | 权重不同的人看同一只票，你怎么不重复算？ | **拆"与用户无关"和"与用户有关"**：bars/缠论/chip/板块/情绪周期是**股票的属性** → L0 共享；因子原始得分也是股票属性；**只有"套权重出总分"是用户属性**（毫秒级）。缓存键用 `(code, 口径指纹)`。（§3.2 铁律、§7.1） |
| 7 | 缓存加了用户维度，命中率不就崩了？ | 崩的只有**个性化那一层**（毫秒级、不需要缓存）。占 99% 成本的行情/因子层键里**没有用户**，命中率不受影响。这是刻意分层：**把用户维度挡在最便宜的那一层**。 |
| 8 | 100 人的 WS 推送怎么扛？ | 现状是**每连接各算一次**（`intraday.py:396`）。改成服务端单一推送循环 + 按 `(code,口径)` 版本号广播 + 每用户版本号单独推自选列表；加连接数上限与慢消费者丢弃。（§7.4） |
| 9 | 多进程之后 **QMT/xtquant** 怎么办？ | **必须单进程独占**。历史事故：xtquant 原生下载接口非线程安全，并发调用导致服务**无 traceback 猝死**；护栏是**进程级** `threading.RLock()`，多副本 = 多把锁 = 护栏失效。要么单进程，要么做 QMT sidecar。（§8.1、ADR D8） |
| 10 | 这套做完**数字**是什么？ | 现状 `实测`：单实例 5~6 请求/秒。`目标`/`设计推演`（P2 完成后）：计算次数降 13×、p95 < 1.5s、16 线程下吞吐 30+ 请求/秒。**没做完就只说目标，不说成结果。**（§9.2） |

### C.2b 三条**专属加分项**（金融科技 / 券商基金岗必问）

> 这三条是普通候选人答不上来的。**答对条款编号与措辞**，比多讲三个技术点更有区分度。
> 依据与出处全部在 §2.5，**别记错**。

| # | 追问 | 应答要点 |
|---|---|---|
| 11 | 你这套合规依据是什么？**具体条款**。 | 分三层说，且**说对名字与编号**：① 法律层——《证券法》**第五十四条**（禁止利用职务便利获取的未公开信息交易，"明示、暗示他人"也算）+ **第一百九十一条第二款**罚则 + **第二百一十四条**（篡改毁损资料设罚，这是"审计不可篡改"的法律落点）；② 规章层——《**证券基金**经营机构信息技术管理办法》（证监会令第 152 号）**第三十二条**（最少功能/最小权限、履行审批流程、开发测试运维分离、岗位相互制衡）+ **第三十条**（数据分类分级）+ **第十六条**（审计报告留存 ≥ 20 年）；③ 自律规则——中证协《证券公司信息隔离墙制度指引》**第十三条**（冲突业务系统"相互独立或逻辑隔离"）、**第十五~十七条**（跨墙审批 + 回墙）、**第十九~二十二条**（观察名单/限制名单）。**再补一句诚实的**：日志留存 6 个月出自《**网络安全法**》**第二十一条**，不是等保条款。 |
| 12 | **撤权之后**，已经发出去的结论/缓存还能被看到吗？ | 这是 ReBAC 里著名的 **new-enemy problem**（Zanzibar 论文提出）：用户被移出组后，仍可能读到**旧 ACL 快照**下缓存的内容。Google 的解法是 **zookie**——把 ACL 快照令牌**随内容一起持久化**，读取时要求"快照 ≥ 该时间戳"。我的落地是**权限版本号**：缓存条目带上写入时的权限版本，请求携带的版本落后即视为未授权（**fail-closed**），权限变更走 Redis 广播失效而**不靠 TTL 等待**。（§8.5.2 第 3 条） |
| 13 | 高权操作你们怎么控？ | **不用常驻高权账号，用 break-glass 临时提权**：限时凭证 + 双人批准 + **自动到期** + 全链路留痕。两个工程细节：① 申请与批准**做成两个 API、两张表**（一个接口做两件事，必然有人把两个字段都填自己）；② **禁止自批放在 DB 层**（`CHECK(approver_id <> requester_id)` + 触发器兜底；注意 `CHECK` 里**不能写子查询**，SQLite 与 PostgreSQL 都不允许）。**注意措辞**：证券期货规章里**没有"双人复核"这个词**，强制依据是第三十二条的"岗位相互制衡"，通用控制定义引 NIST SP 800-53 **AC-5**。 |

### C.2c 一条**数据源容量**加分项（做实时系统的岗必问）

> 这条来自 `scripts/_probe_datasource.py` 的实测。**多数候选人答不出"瓶颈在哪个接口"**，
> 只会说"加缓存"。能报出接口级约束的人，一眼就能看出真做过实时链路。

| # | 追问 | 应答要点 |
|---|---|---|
| 14 | 100 人同时用，**实时行情取数**扛得住吗？ | 先给**接口级实测**：批量快照**一次 ≤60 只、53ms**；而 5 分钟 K 线与分时**只能逐只取**（实测多代码请求回 41B、0 bar）—— 单只 5mK **24KB / ~100ms**、分时 **9.3KB / ~80ms**。然后给**关键推论**：数据源压力只与**去重后的不同股票数**成正比，与人数无关 —— 100 人各 20 只自选，去重后约 400 只（热门票高度重叠），不是 2000 只。最后给方案：**活跃宇宙（所有自选并集）+ 分档 TTL**，报价 5s（批量 7 请求/0.4s）、分时 60s、5mK 300s —— 合计 **~3 请求/s、<0.5 核**。结论：**数据源不是瓶颈，重复取数才是**。（§7.2、§8.5.4） |
| 15 | 那你怎么保证"用户点开就快"？ | **预取 + 内存命中**：刷新循环先把宇宙取到内存，用户请求命中缓存（实测 `/watchlist` 命中态 **0.05s**）；长尾票走 stale-while-revalidate 先给旧值。**绝不在用户打开页面时才下载 320 根 5mK**（那是 24KB + 100ms × 重复人数）。 |
| 16 | 短时间大量新用户涌入怎么办？ | 三道闸门：① **宇宙并发闸门**（单源 ≤8~16 并发）+ 域名令牌桶；② **套餐分档节流**（试用 30s/180s 才刷，VIP 5s/60s）—— 节流手段是**套餐**而不是代码里写死；③ **长尾降级**（宇宙 >1500 只时冷门票降到 15 分钟档）。并且 **fail-closed**：数据源抖动时限流排队，绝不"100 个用户同时冷取 800 只票"。 |

### C.2d 两条**健壮性**加分项（架构/后端岗必问）

> 来源：需求方明确提出"**子功能崩溃不要整站崩、取不到数据自动换源、换不到就上报**"。
> 这两条是**区分"能跑通"与"能上线"的分水岭**，而且答得出来的人不多。

| # | 追问 | 应答要点 |
|---|---|---|
| 17 | 某个子功能挂了，怎么保证**不整站崩**？ | 分两层答。**第一层是执行边界**：`asyncio.gather(..., return_exceptions=True)` + 逐个兜异常，失败进 `gaps` 且该因子从 `available_weight` 扣除 —— 一个维度挂了，面板照出数（现有代码已这么做）。**第二层是进程边界**：这才是真问题 —— 现状全模块在**同一个 uvicorn 进程**里，历史上 `xtquant` 并发下载导致过**无 traceback 猝死**（整站崩）。所以要做：按资源特征拆进程（api/realtime/worker/scheduler/qmt-sidecar）、模块级降级开关、危险调用子进程化、`--limit-max-requests` 定期重启兜内存泄漏。（§7.6.1、§8.2） |
| 18 | 并发改的状态怎么保证不出竞态？ | 用 **Actor 模式**把"状态修改"收敛成单点：`next_state = f(current_state, input)`，外部只能投消息、不能碰状态。**为什么不用裸锁**：锁只解决"不冲突"，解决不了"修改点散布各处"（散在业务 catch/finally/回调里），无法审计所有修改点、无法重放，异步回调还能偷偷改状态。Actor 化后每个状态转换都有 `input → output` 边界、单条消息失败不影响循环（**这本身就是故障隔离**）。但**只对有竞态的状态用**（`SourceHealthTracker`、`_watch_cache`），全盘 Actor 化会让所有访问异步化 + 邮箱积压。（§7.6.2） |

### C.3 双岗侧重：同一份设计，两种讲法

| 设计点 | 投**量化开发**怎么讲 | 投**AI 应用开发**怎么讲 |
|---|---|---|
| 三级资源模型（§3） | **数据的归属决定正确性**：个股的"股性"是股票的属性，因子权重是用户的属性——混在一起就会出现"我的参数算出别人的分"，这在实盘是**会亏钱**的 bug | **状态分层**：哪些是模型无关的中间结果（可缓存/可共享），哪些是请求级个性化（快、别缓存）。这与 LLM 应用里 prompt/上下文/tool 结果的分层同构 |
| 口径指纹作缓存键（§3.2） | 因子计算的**可复现性**：同一 `(标的, 参数, 日期)` 必须得到逐位一致的分数，与谁在算无关 | **确定性输入 → 可缓存输出**是 LLM 应用降本的第一性原理；语义缓存只是它的模糊版本 |
| 重任务队列 + 配额（§7.3） | 回测/选股是**批处理资源**，不该和实时信号抢 CPU；交互式（拟合档位）与批量（全市场选股）分级 | **LLM 调用是最贵的外部资源**：队列 + 每租户 token 预算 + 熔断器保护它（本项目已有网关，这里补"按用户配额"） |
| **活跃宇宙取数调度**（§7.2） | 因子计算的输入（bars/分时）是**公共数据**：100 人看同一只票只该取一次。报价能批量（60 只/53ms），分钟线只能逐只（24KB/100ms）—— 所以调度粒度必须是**标的**而不是**用户** | **同一份上下文只构造一次**：把"取数/embedding/工具结果"从"请求级"下沉到"资源级"，是 LLM 应用真正降本的地方（比换更小的模型收益更确定） |
| 套餐层级 vs 角色正交（§4.6） | 层级管**量**（自选上限/刷新频率/配额），角色管**权**（能力码）。混在一起会出现"付费即超管" | **配额与权限是两套策略**：LLM 应用里"这个租户能用多少 token"与"他能调哪些工具"也必须是两件事，否则升级套餐就会顺带提权 |
| 审计与隔离墙（§5.3、§2.5、§9.4） | 机构合规的真实要求：留痕、**岗位相互制衡**（别说成"规章要求双人复核"）、研究/投资隔离（指引第十三条） | **AI 输出的可追溯性**：prompt_hash + 数据来源 + 谁在什么权限下看到哪个结论——金融 AI 落地绕不开 |
| 两份实测→否掉加副本（C.1、C.2c） | **量化思维**：先测量再优化，分清"单轮成本"与"重复次数"；瓶颈要定位到**接口级** | **工程判断**：能识别"加机器解决不了的问题"，而不是堆资源 |

### C.4 履历条目（实现进度决定可写版本）

> ⚠️ 与 `RESUME_PROJECT.md` §D 的纪律一致：**没实现的不要写成已实现**。

**现在（P0 之前）可以写** —— 都是"设计与实测"，不是"上线成绩"：

```
· 高并发改造（设计+实测）：定位到单实例吞吐饱和 5~6 req/s 的根因是
  「100 人看同一只票被计算 100 次」+「pandas 计算跑在事件循环里」，
  实测整机 CPU 仅 6~13% → 判定瓶颈是重复计算而非核数，据此否掉"加实例"方案，
  设计「共享因子层 + 口径指纹缓存 + 重任务队列」三层方案，目标计算次数降 13×；
· 实时行情供给设计（接口级实测）：实测批量快照一次 ≤60 只/53ms，
  而 5 分钟 K 线与分时只能逐只取（24KB/~100ms、9.3KB/~80ms），
  据此推出"数据源压力只与去重后标的数成正比、与人数无关"；
  再按「每人 5 池 / VIP 100 只 / 试用 25 只」算出未去重 3.76 万条、
  并经需求方确认去重宇宙 **2,000 只**，
  并发现"全宇宙分时 @60s"会饱和（50 只/s）→ 设计「F 前台 / H 热门后台 / L 长尾」
  三档 + 盘前预热（09:15 起 10 只/s 分批），把峰值压到约 9 请求/s、<0.6 核；
· 平台级多租户设计：五维正交权限（成员/RBAC/ABAC/隔离墙/证券名单）+
  模块级能力码 + PostgreSQL RLS 双层隔离（应用层下推 + FORCE RLS），
  并识别出连接池复用导致租户串号的经典陷阱，将其列为必做项而非优化项；
· 套餐分级与个性化参数：层级（管理员/VIP/试用）管配额与刷新频率、角色管能力码的
  正交设计；个性化参数用 copy-on-write（有自定义才落库，否则回退系统默认），
  最坏 17,600 行 / <50MB。
```

**P1 完成后追加**（那时才是"已实现"）：

```
· 落地用户级隔离：自选池/个股参数档案迁移至 (tenant_id, user_id, code) 主键 +
  行级 RLS；补 15 项隔离与合规回归测试（含"缓存键必须含用户或口径指纹"的 AST 级检查）。
```

**P2 完成后追加**（有实测数字才写）：

```
· 做T实时链路改造：同标的请求单飞 + 因子层与个性化层分离，
  实测 p50 由 6.5s 降至 __（待补），并发 100 路 WS 下 p95 < 1.5s（待补）。
```

> **一句话原则**：**"设计 + 实测瓶颈" 现在就能讲，而且更值钱；
> "已上线的性能数字" 必须等 P2 跑完再写。**
> 面试官验证成本最低的就是数字——写在简历上的数字一定会被追问怎么测的。

### C.5 诚实边界（主动说，比被问出来强）

| 边界 | 说法 |
|---|---|
| 不是分布式系统 | "单写者 + 只读共享卷，进程拆分按资源特征，不是微服务" |
| 没有真实资金验证 | 沿用 `RESUME_PROJECT.md`：不写任何收益/胜率数字 |
| 多租户是**设计稿** | "已落地的是权限底座（Principal/policy/RLS 生成器）与实测瓶颈；用户级隔离是 P0/P1 待实施" |
| 控制台隔离只到 L1 | "指引第十三条允许'逻辑隔离'，我做到逻辑隔离；物理隔离留给真有多部门部署时" |
| 不含交易拦截 | "限制名单只作用于研究与自选侧，下单链路在 `qmt/`，不声称实现了交易合规拦截" |
| LLM 侧只到"可追溯" | 不声称解决幻觉；成本控制靠分层路由 + 配额 + 缓存 |
| 100 人是**目标规模** | 当前 `实测` 支撑 5~6 req/s；目标值有推导过程（§7.1），不是拍脑袋 |

---

## 附录 D · 需求口径确认（**已确认，作为设计输入**）

以下三条于 2026-09 由需求方确认，本文所有容量估算与数据结构均以此为前提。

| # | 口径 | 确认内容 | 影响的设计 |
|---|---|---|---|
| 1 | **并发形态** | **100 人同时活跃操作**（不是"同时在线挂机"） | 按最坏情况估算：100 路 WS + 活跃切换；§7.1、§7.2、§8.5.4 |
| 2 | **租户划分** | 租户 = **套餐层级**：**VIP 客户 / 试用用户 / 管理员**；不同层级给不同权限 | §4.1（第 1 维语义升格）、§4.6（层级管"量"、角色管"权"）、§5.1 `dim_tier` |
| 3 | **自选上限** | 管理员 ≤ **100** 只、VIP ≤ **20** 只、试用 ≤ **10** 只 | §4.6 服务端上限校验；§7.2 活跃宇宙规模由此推出 |
| 4 | **个性化参数** | 与**用户**绑定：有自定义就保存、没有就用系统默认；**用户改动不得影响他人** | §3.3（copy-on-write 继承）、§4.6 ③、§5.3 `dim_intraday_profile_v2` 主键含 `user_id` |
| 5 | **调权重的票数** | **很多**（用户会为大量标的单独调参） | 已量化：S2 最坏 **17,600 行 / < 50MB**（远期 S3 才 37,600 行 / < 100MB）（§4.6、§7.2.6⑥），**不构成存储压力**，不需特殊结构 |
| 6 | **使用时段** | **短时集中：绝大多数在 09:20–10:30**（约 70 分钟） | **推翻"按日均摊"**：负载是**尖峰型**，必须按峰值设计与盘前预热（§7.2.4） |
| 7 | **健壮性要求** | 子功能崩溃**不得整站崩**；取不到数据**自动换源**；全源失败要**上报数据源异常** | §7.6（降级矩阵 + 两层隔离 + 异常上报表）；ADR D14/D15 |
| 8 | **合规要求** | 开发测试运维分离；注意整体合规性 | §8.7（三环境 + 启动自检拒绝不自洽配置）、§2.6（条款级依据）、§4.4/§9.4（名单与控制台隔离） |
| 9 | **注册方式** | **必须系统管理员审批**才能通过 | §8.6（`pending` 五态状态机 + 审批流水 + 禁止自审）；依据指引第八条"需知原则" |
| 10 | **隔离粒度** | **actor 原子级隔离** | §7.6.2（Actor 模式收敛状态修改，借鉴 `moss-finance-assistant`） |
| 11 | **负载与容灾** | 做好负载均衡与容灾设计 | §8.8（无状态接入 + 有状态收敛；容灾按 RTO 分级；演练清单） |
| 12 | **参考项目** | `D:\code\moss-finance-assistant`（仅供参考） | §2.4（借鉴 Actor 模式、SLO/熔断常量、RBAC 策略表含限流；并指出它的两处短板） |
| 13 | **自选池结构** | **每人 5 个自选池**；VIP 共 100 只、试用 5×5 = **25 只**；另有**用户自定义板块股池** | §4.6（`pool_limit`/`pool_size_limit`/`sector_limit` 三重校验）、§5.3 `dim_user_pool`、**§7.2.6 全面重算** |
| 14 | **用户生命周期** | 管理员有**增加/删除**权限；**每个用户都有使用有效期**；要一个**管理员管理租户的界面** | §8.6.1（七态状态机）、§8.6.1.2（有效期四段机制）、§8.6.1.3（软删除/匿名化）、§8.6.10（管理员控制台）、§8.6.10.5（个人配额破例） |
| 15 | **自选池涨跌幅要实时刷** | 左侧自选股池的涨跌幅要**实时刷新**；这是**公共数据、可实盘共享**，只是**分发给多个用户** | **§7.2.6④/④b**：报价**取消层级分档、全宇宙统一 5s**（实测批量 300~500 只/请求）；并把"取数共享"与"分发按用户"显式分开 |
| 16 | **人数口径** | **当前 VIP 30 人，很快扩容；试用用户至少百人** | §7.2.6②/③b：按 **S2（VIP 100 / 试用 300）** 做设计点，S1 当前、S3 远期各列一档 |
| 17 | **自定义板块进宇宙** | 自定义板块**要进宇宙**，但**刷新频率降一个数量级**；**只在服务器/带宽有余量时**自动更新 | **§7.2.6⑤/⑤b/⑤c**：P0 当前池（5s/60s）· P1 背景自选（5s/300s）· **P2 冷板块（5s/900s/1800s，余量门控）** |
| 18 | **每用户只有一个池实时** | 用户切到自定义板块时，**实时刷新切到当前查看的板块**；"一个用户最多同时只有一个股池要实时刷新" | **§7.2.6⑤b**：把"宇宙"与"实时集"解耦 —— 宇宙 3,500 只，**实时集只 ≈1 个池/人** |
| 19 | **远程发布** | 通过 **Cloudflare Tunnel** 远程共享给用户网站 | **§7.5**：拓扑 + 六个坑（WS 保活、真实 IP、隧道≠认证、内网信任头剥离…）+ 发布前安全清单 |
| 20 | **账号与认证功能** | 注册 / 登录 / 改密码 / **记住密码** / **绑定手机号** / **通过手机号找回** | **§8.6.4~8.6.9**：认证数据模型（5 张表）· 六条流程 · **6 个安全要点** · 通知通道 · 前端要点；ADR **D25~D28**；验收测试 **40~52** |
| 20b | **通知通道（本轮定）** | 🔒 **只用邮箱，短信能力后置** | §8.6.8④：注册/找回**全部走邮箱**；手机号降为"选填联系方式，不验证、不参与找回"；`dim_user_contact` 与通道裁决**按多渠道建好**，将来加短信**不改表不改流程**；ADR D32 |
| 21 | **会话认证与 sessionId 隔离** | 支持 **sessionId 级信息隔离**；**短期 Cookie 认证**；**短期不看、同设备再打开不用输密码** | **§8.6.11**：两层凭证（session 30min 滑动 + 12h 绝对上限 / refresh 7d）· Cookie 三条 · 隔离什么与不隔离什么 · 三处代码落点；ADR **D29~D31**；验收测试 **53~60** |
| 22 | **"短期不看"的时长口径** | 🔒 **已确认：7 天免密窗口** —— 间隔 ≤7 天、同设备再打开**不用输密码**；>7 天才要求重新认证 | **§8.6.11.1**：两层凭证（session 30min 滑动 + **12h 绝对上限** / refresh **7d 滑动**）；配套 `/auth/sessions` 可查可踢 + 改密撤销全部 refresh token |

**由 1、2、3、13、15~18 推出的活跃宇宙规模**（§7.2 的输入，**已三次重算**）：

```
未去重自选条目：
  1 管理员 × 100 + 100 VIP × 100 + 300 试用 × 25 = 17,600 条（S2 设计点）

去重后宇宙（关键：多池 ≠ 共享股票池，5 个池内是不同的票）：
  ★ 自选池部分  ≈2,000 只   ← 需求方确认
  ★ 自定义板块  ≈1,500 只   ← 需求 17：板块进宇宙（但降频）
  ─────────────────────────
  ★ 合计宇宙    ≈3,500 只

实时集（需求 18 的关键约束）：
  每用户**最多 1 个池**在实时档 → 100 人 ≈ 100~200 只走 P0/60s
  这与"每人 21 个池"的差别是 20 倍 —— **实时集只跟"在看什么"有关，与"有多少池"无关**

⚠️ 演进记录（避免再引用旧数字）：
   v1 "300~500 只 / 最坏 800 只"      → 作废（没算到每人 5 个池）
   v2 "3,000 只（仅自选）"            → 悲观估算
   v3 "2,000 只（仅自选）"            → 人数口径修正后
   v4 ★ "3,500 只（自选 2,000 + 板块 1,500）" → 当前（板块进宇宙）

数据源负载（v4，实测外推）：≈13 请求/s（能力 ≈130 req/s，**余量 ≈10×**）、
  < 0.6 核、宇宙缓存 ≈115MB、下行 < 6 Mbps。
分时档**只给 P0 当前池（60s）+ P1 背景自选（300s）+ P2 冷板块（900s）**；
报价**不分档、统一 5s**（批量接口不稀缺；§7.2.6④b）。
盘前预热窗口 09:15–09:30 **已确认可接受**（3,500 只 5mK ≈ 84MB / 约 8 分钟）。
```

**仍需在实现时确认的（不影响架构，影响参数）**：

1. VIP / 试用的**实际人数分布**？（本文按"VIP 300 + 试用 300"做最坏估算；
   若 VIP 只有 20 人，宇宙会显著低于 2,000 只。）**这条仍是最大的不确定项**。
2. **自定义板块股池是否进数据宇宙**？（本文决策：**不进**，只作研究分组；
   要实时监控须加入自选池 —— 否则 2,000 只会变 10,000+，§7.2.6⑤）
3. 试用用户是否允许**自定义参数**？还是只能看系统默认？
   （影响 §4.6 的 `profile_limit`：若试用不可自定义，则 `profile_limit = 0`。）
4. 套餐**升级路径**：试用（25 只）→ VIP（100 只）时，试用期积累的自选与参数是否保留？
   （按 §3.3 copy-on-write，**建议保留**：换 `tenant_id` 而不动 `user_id` 即可，
   数据天然跟着人走；且**上限是放宽的**，不需要迁移或裁剪。）
