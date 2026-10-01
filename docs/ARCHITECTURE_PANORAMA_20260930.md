# 投研分析多 Agent 协作 · 全景架构与规则（2026-09-30 重新生成）

> **这份文档的纪律**：每一个数字、每一条规则都**从代码/配置现读**（可复跑命令见文末），
> 不凭记忆、不写"应该是"。凡我读不到的，一律写"未确认"。
>
> 目的（用户口径）：重新衡量**当前设计还有哪些不足**、以及
> **数据能否永久最大可能地自己采集闭环**，而不是不停地补方法、补数据匹配策略。

---

## 一、一页看懂全景

```
                        ┌─────────────────────── 交互入口 ───────────────────────┐
                        │ POST /analyze （target=标的, query=问句, analysis_type）│
                        │   ├─ resolve_analysis_subject()  标的与问句冲突时以问句为准
                        │   └─ QueryDeadline（防撞钟 10s，只挂交互路径）
                        └───────────────────────────┬───────────────────────────┘
                                                    │
                ┌───────────────────────────────────▼───────────────────────────────────┐
                │                     编排层 Supervisor（LangGraph StateGraph）        │
                │  规划：LLM 规划  ⊕  规则规划  →  增补链（查询信号/宏观必查项/流动性/   │
                │        行业路由/渗透率）→ 后缀形态判据 → 白名单过滤 → payload(+hint)   │
                └───┬───────────────┬────────────────┬───────────────┬────────────────┘
                    │               │                │               │
        ┌───────────▼──────┐  ┌─────▼──────┐  ┌──────▼───────┐  ┌────▼─────────────┐
        │ ① 采集 A01–A04   │  │ ② 信息 A05–A07 │  │ ③ 分析 A08–A12 │  │ ④ 行业 A13–A16   │
        │ 确定性流水线      │  │ 校验/抽取/情绪  │  │ 宏观/中观/微观 │  │ 科技/消费/周期/药 │
        └───┬──────────────┘  └────────────────┘  │ /风险/合规     │  └──────────────────┘
            │                                     └──────┬───────┘
            │                                            │
   ┌────────▼─────────┐                       ┌──────────▼─────────┐
   │ 数据获取四跳      │                       │ 计算（算出来的）    │
   │ ①本地目录(列索引) │                       │ 规则计算 + 派生公式 │
   │ ②本地库(索引/事实)│                       │ + 平台自有族        │
   │ ③连接器路由(24个) │                       └──────────┬─────────┘
   │ ④联网兜底(白名单) │                                  │
   └────────┬─────────┘                                  │
            │                                            │
   ┌────────▼────────────────────────────────────────────▼─────────┐
   │ ⑤ 决策 A17 推荐  ⑥ 审计 A18  ⑦ 缺口自愈 A19（LLM 生成连接器）  │
   └────────┬──────────────────────────────────────────────────────┘
            │
   ┌────────▼──────────────── 数据闭环（不需要人）────────────────────┐
   │ 调度 48 个作业 → catalog 批采(4 个/84 指标, 按登记频率)          │
   │ → 定期维护审计(每日 07:45) → 缺口队列(3 路由) → 取数侧补采       │
   │ → 失败月频重试(不放弃) → 索引回填 → 数据采集异常(管理员可见)     │
   └──────────────────────────────────────────────────────────────────┘
```

**一句话**：链路是"**四跳取数 + 两条规划 + 三类计算 + 五道闭环**"；
**人只在四个点上被需要**（见 §六）。

---

## 二、硬数字（全部现读，可复跑）

| 维度 | 数字 | 来源 |
|---|---|---|
| Agent（`A0x_yyy` 形态） | **19 个**：A01–A18 + A20 —— **注册处实测 20 条**（`src/api/runtime.py:364-390`，含 `A19_code_engineer`） | `supervisor.py` 正则 + 分面清点 |
| `supervisor.py` 有效代码行 | 3,900+（单文件，**高扇出**） | `_panorama_facts.py` |
| 连接器文件 | **24 个**；其中 **19 个**声明 `indicators`，声明条目合计 **41** | `src/infrastructure/connectors/*_connector.py` |
| 登记指标 | **122 条**（启用 **117**，其中**模板 28**） | `IndicatorRegistry.all()` |
| 频率分布 | daily 46 · quarterly 36 · monthly 21 · realtime 9 · weekly 3 · intraday 2 | 同上 |
| 分类分布 | individual 52 · cn_industry 25 · mkt_liquidity 14 · cn_macro 9 · macro 6 · fed 5 · calendar 5 · us_macro 1 | 同上 |
| 派生指标（公式） | **1 条**：`净息差:{code}` | `configs/derived_indicators.yaml` |
| 调度作业 | **48 个** | `JOB_REGISTRY` |
| catalog 批采覆盖 | 4 个作业 / **84 指标**（daily 20 · monthly 30 · quarterly 31 · weekly 3） | `plan_jobs()` |
| 缺口队列 | pending 44 · resolved 27 · skipped 11 · failed 2 · total 84 | `GapQueue.stats()` |
| 测试护栏 | **322 个测试文件 / 74,315 有效代码行** | `tests/**/test_*.py` |

---

## 三、设计不足清单（按"能否机器闭环"排序）

> 评级口径：**A** = 会影响结论正确性；**B** = 会造成维护负担/漂移；
> **C** = 体验/可观测性问题。
> "能否闭环"= 该不足**能否只靠机器**消除（不需要人给新输入）。

| # | 不足 | 症状（实测证据） | 根因 | 级 | 能否机器闭环 |
|---|---|---|---|---|---|
| 1 | **换源依赖人** | `社融` 源停更（商务部镜像停在 `202604`）、`us_*` 五条源停更 —— 处置都是"人去找替代源" | 全仓库**没有任何自动换源逻辑**；`ConnectorRouter` 只做**同一指标内的多源故障转移**，不做"这个源死了⇒换一个不同口径的源" | A | **否**（结构性）→ 见 §七 提案 |
| 2 | **口径一致性没有机器校验** | `fred:PAYEMS` 是水平值、`fred:CPILFESL` 是指数 —— 这些**只能人工写进** `_MACRO_BASIS`；写错会产出"看着有据的错误数字" | 口径是**语义**，代码里没有"新序列的口径声明"的强制字段；换源后更无校验 | A | **部分**（可强制声明 + 量纲/频率自动核对） |
| 3 | **新指标的登记仍需人** | 加一条 FRED 序列要改 `indicators.yaml`；加一个连接器要写类 + 登记 | 登记表是**唯一真值源**，而它由人维护（刻意的：公式/出处必须人复核） | B | **否**（刻意保留），但可把"人只写一行"做到极致 |
| 4 | **"数据事实"仍散落** | A08 prompt 里的"有哪些数据"枚举已漂移（美国那半边还是 `us_*` 时代族名）——本轮才删掉；A09–A20 **未体检** | 同一事实写在 prompt/注释/判据/登记表多处 ⇒ 必然漂移 | B | **是**（分类判据已留：答案在代码里就移出 prompt） |
| 5 | **判据入口数量是风险源** | 新增美国侧触发词时**差点破掉** `news` 不拉数据层的闸（`test_news_graph_info_pipeline` 当场红） | 同一个判断有多个入口（规划、增补、payload、白名单），每加一个入口就要重问一遍闸门 | A | **是**（把闸门收敛成单点 + 派生断言） |
| 6 | **散文缺口缺上下文** | 44 条 pending 里 6 条 `unmapped`：2 条缺标的代码、1 条分析级、2 条老裸名 | A17 报缺口时只给一句人话，**没带标的/维度**，事后无法还原 | B | **是**（在报缺口那一刻带上 `code`/`dimension`） |
| 7 | **三档事实表易写错目标** | 本轮实测：只设 `MOSS_ENV=pilot` 时补数**写进了主库**，用户看的那一档还是空的 | 隔离开关是 `MOSS_SQLITE_PATH` 而非 `MOSS_ENV`；脚本没有"先打印目标库"的自证 | A | **是**（已要求所有补数脚本先打印库路径；可做成强制） |
| 8 | **realtime 类只有月频下限** | 9 条 `realtime` 指标原先**一个作业都没有**；现并入月频兜底 ⇒ 最坏一个月才更新一次 | 频率声明与"我们实际能多快刷新"是两件事 | B | **是**（按源能力声明 `refresh_floor`） |
| 9 | **横截面/多值语义靠判据兜** | `fed:policy_range` 同日三值（上下限/有效利率）曾把**下限当政策利率**报出 | 一个 indicator 承载多语义；已拆成三条单值序列，但**新族仍可能重犯** | A | **部分**（登记时强制 `value_semantics`） |
| 10 | **运维信息与用户信息刚划界** | 本轮才把采集缺口/防撞钟从用户界面移走；LLM 慢调用等仍在别处 | 展示层缺少统一的"信息分级"约定 | C | **是**（已在做，未覆盖全部类别） |
| 11 | **"停产"只落在登记面** —— `akshare_connector` 的能力表里仍列着 6 条已停产的 `us_*` ⇒ `supports()` 仍为真，按老名字请求会拿到**一年多前的值且不报错** | B | 是（能力面同步标停用） |
| 12 | **形态型连接器在能力枚举里不可见** —— `fred_connector` 的 `indicators` 是空表，靠 `supports_template` + 形态判据取数 ⇒ 任何"按能力枚举候选源"的机制**看不到 FRED 序列** | B | 是（为形态型源单独设计枚举） |
| 13 | **★ 采集路径的联网兜底从未执行（本轮修掉）** —— `_fallback_fetch` 签名无 `state`，而采集调用点多传 `state=state` ⇒ 每次 `TypeError` ⇒ 被宽 `except` 以 `debug` 吞掉 ⇒ 恒返回 `[]`。文档口径「找不到就去联网找」在采集侧**等于没接**（`grep tests/` 对该路径**零护栏**） | **A** | **已修 + 已配护栏** |
| 14 | **规划层规则路径无审计** —— `plan_run` 没有任何审计写入 ⇒ "走了规则规划"与"压根没规划"在审计里长得一样（与分析层当年那个缺陷同形状，规划层未修） | B | 是（照 `_audit_rule_only` 补一条） |
| 15 | **文档口径与代码事实漂移** —— Agent 数：runtime **20** / agents.yaml **19** / AGENTS.md **18**；`supervisor` 两处注释与代码不符（分析层是否串行在 `liquidity_ctx` 之后、docstring 里 3 个 subgraph 函数并不存在） | C | 是（口径以代码为准改写） |

---

## 四、数据获取面（规则总表）

> 分面清单（连接器逐条、顺序、护栏、落库链、平台族、存储隔离）见
> §八「子面清点（代码现读）」——每条带 `文件:行`。

---

## 五、匹配与计算面（规则总表）

> 同上，见 §八。

---

## 六、闭环与自愈面

> 同上，见 §八。

---

## 七、"数据能否永久最大可能自采闭环"——诚实评估

### 7.1 现状：**半自动闭环**

| 环节 | 现状 | 需要人吗 |
|---|---|---|
| 已有活源的指标：采集→清洗→校验→入库→索引→维护审计→缺口入队→取数侧补采→月频重试 | **全自动**（48 个作业 + 4 个批采 + 每日维护审计） | **不需要** |
| 新增"按 id 精准连接"的序列（FRED 式） | 登记**一行**即进入全链 | 需要人写**一行** |
| 新指标（有登记表条目 + 连接器/通用连接器可认） | 自动进计划、自动进批采、自动进维护审计 | 需要人登记 |
| 源停更 | 能**机器判出**（"取到但与库内同期"= `source_lag` 证据）+ 月频重试不放弃 | **需要人换源** ← 唯一的硬缺口 |
| 全新概念（如"银行息差"） | 有派生流水线（概念→公式→数据名→取数→计算） | 需要人写公式与**出处**（刻意：防"看着有据的错误数字"） |
| 物理不可得（如北向资金停止披露） | 已能机器标记 `terminated` 并**停止重试** | 不需要（但首次判定靠复核表） |
| 跨体系名称（名录 110 vs 板块池 457） | 逐条复核表 + 双向过期检查 | 需要人复核（**已否决**模糊匹配：错配比缺数据更危险） |

**结论**：**"已知源的维持"已经闭环了**（这一环不再需要人）；
**"源的替换/新增/口径判定"仍需人给一次输入**。所以现在的形态是
**半自动闭环**：不是"不停补方法"，而是"**每次源死掉要人抬一次**"。

### 7.2 要把"换源"也做成数据侧机制（提案，未实施）

现在 `ConnectorRouter` 的能力是"**同一指标、多个已声明源之间**故障转移"。
缺的是"**这个指标的所有已声明源都死了 ⇒ 自动找新源**"。可复用既有零件拼出来：

```
① 停更判定（已有机器证据）
   drain_catalog_gaps() 的 "取到但与库内同期" + maintenance 的 "停了才算"
   ⇒ 连续 N 个期望周期都取不到新期 ⇒ 标 source_dead（写进 extra，随数据下发）

② 候选源探测（新，但复用 supports()/通用连接器）
   对该指标枚举：其它连接器 supports() ∪ 通用连接器族（fred:<SERIES> 式）
   逐个真取一次，得到候选序列

③ 口径一致性校验（新，这是关键闸）
   比对：频率（期间间隔中位数）/ 量纲（量级与单位）/ 最近期 / 与旧序列的重叠期相关性
   ⇒ 通过才允许切换；不通过则**只登记候选**，不自动切
   （为什么必须卡这一步：本项目实测「指数」被当「同比」、「水平值」被当「新增」，
     错口径比缺数据更危险 —— 数字看着有据）

④ 影子期 + 切换
   新源先与旧源**并行取数 N 期**，逐期比对一致后升为主源、旧源降兜底；
   口径差异写进 extra.basis；切换动作落审计（谁在什么时候换了源）

⑤ 换源也要有"出处"
   与派生公式同一条纪律：新源必须带 sources（URL/序列号），否则不许升为主源
```

> **进度（2026-09-30，`CHG-0110`）**：**路径 A 已落地** ——
> `catalog/source_reroute.py` 实现了「探测已有连接器 → 口径一致性校验 →
> 影子期 → 落覆盖层（主源 + 两个备源）」，并接在盘后补采的「源停更」触发点上；
> **路径 B（LLM 提议 URL）与 C（真搜索引擎）未做**，见 PRD §19.29.6。

**这套做完，"源停更"就从事务性工作变成一条日志。**

**已有零件（提案不是从零开始，逐条给锚点）**：

| 零件 | 现成的东西 | 文件:行 |
|---|---|---|
| 停更证据 | `ConnectorRouter._stale_floor`：记住"某源上次返回的那份数据 + 它的最新日期"= **源给不出更新期的直接证据** | `src/infrastructure/connectors/router.py:322`、`:796` |
| 失败/冷却 | `_failure_cache`（按 连接器×指标 记失败与冷却）、`source_cooldown.py` | `router.py:316`、`src/infrastructure/connectors/source_cooldown.py` |
| 候选源枚举 | `ConnectorRouter.supports()` / `_matched()` 能列出**所有**认这个指标的连接器 | `router.py:333`（`_matched`）、`router.py:337`（`supports`） |
| 源健康排名 | `intraday/source_health.py` 的 `SourceHealthTracker`（`last_success_at` + `rank()`），目前只服务分钟链 | `src/intraday/source_health.py:96`、`:281` |
| 运维可见性 | 管理员「数据源健康度」已把这些暴露出来 | `src/api/data_health.py:197`、`src/api/routes/admin_platform.py:580` |

**预计改动点**：`ConnectorRouter`（新增"跨源探测"）、`maintenance.py`（新增 `source_dead` 状态）、
`gap_queue.py`（新增 `ROUTE_REROUTE`）、`indicators.yaml`（候选源声明）、
以及影子比对结果落到既有的采集异常区（§19.27.4）。

### 7.3 仍然**必须**由人决定的四件事（不建议自动化）

1. **新概念的公式与出处**（`derived_indicators.yaml` 的 `sources` 硬要求）——
   公式错会产出"看着有据的错误数字"；
2. **两套分类体系的名称对齐**（`_BOARD_NAME_ALIASES`）—— 已实测否决模糊匹配；
3. **物理不可得的首次判定**（北向资金这类）—— 需要读公告；
4. **口径等价性的最终裁定**（§7.2 ③ 的校验只能"报警"，不能替人拍板）——
   这也是**唯一**建议保留人工的位置。

---

## 八、子面清点（代码现读，每条带证据）

> **8.0 基线**由本次会话**逐处直接读码**得到（下面每条都标了 `文件:行`）；
> 四个只读分面清点（编排/采集/匹配计算/闭环自愈）会在此基础上补细节，
> 若有出入**以代码为准**。

### 8.1 编排面

| 规则 | 判据/实现 | 证据 |
|---|---|---|
| 标的与问句冲突时**以问句为准** | 输入是裸 6 位代码且问句解析出**不同**股票 ⇒ 改 `target` 与 `focus_stock_code`，说明进 `progress` 首条 + `subject_note` | `src/api/routes/research.py::resolve_analysis_subject` |
| 防撞钟**只管交互路径** | `QUERY_DEADLINE_SEC`（默认 10s）；定时/预热路径**刻意不传** | `src/core/intel_limits.py`、`src/intraday/warm.py:67` |
| 宏观**必查项 13 条** | `ensure_macro_indicators()`：中国四件套 + 美国/利率 9 条；`news` 显式跳过 | `src/orchestration/supervisor.py`（`_MACRO_INDICATORS`/`_US_RATES_INDICATORS`/`_US_RATES_KEYWORDS`） |
| 口径随数据下发（不写 prompt） | `_MACRO_BASIS` + `macro_basis_for()` → `payload.hint["macro_basis"]`；缺口同理 | 同上（`_MACRO_BASIS`、payload builder 的 `macro_required_missing`） |
| 白名单按 **en_id / 族前缀**匹配 | 中文标签一条都放不过；`A08_macro` 已放行 `fed:` 与 `fred:` | `supervisor.py::_AGENT_DATA_WHITELIST` |
| 名字两种错都挡 | 后缀多了：`strip_bogus_code_suffix()`（派生自登记表）；后缀少了：`_needs_code_suffix()` = 显式集 ∪ 现读目录的"需带代码后缀" | 同上 |

### 8.2 采集面

| 规则 | 判据/实现 | 证据 |
|---|---|---|
| 四跳取数 | 本地目录(列索引) → 本地库 → 连接器路由 → 联网兜底(fail-closed) | `catalog/column_index.py`、`catalog/local_data.py`、`connectors/router.py`、`catalog/network_fallback.py` |
| 连接器规模 | 24 个文件 / 19 个声明 `indicators` / 41 条声明 | `_panorama_facts.py` §② |
| 源健康与记忆 | `_stale_floor`（源给不出更新期的证据）、`_failure_cache`（连接器×指标 失败冷却） | `connectors/router.py:322`、`:316` |
| 通用连接器（加一条序列只登记一行） | `fred:<SERIES_ID>` 形态即认；空值行跳过不填 0 | `connectors/fred_connector.py` |
| 落库链职责 | A01 采集 → A02 清洗 → A03 校验 → A04 **幂等**入库 | `domain/agents/data/*/agent.py` |
| 三档隔离的开关是 `MOSS_SQLITE_PATH`（**不是** `MOSS_ENV`） | `is_main_instance()`：没注入 `MOSS_SQLITE_PATH` ⇒ 判主实例 ⇒ per_env 存储解析回主库 | `infrastructure/catalog/data_stores.py:164`、`:99` |

### 8.3 匹配与计算面

| 规则 | 判据/实现 | 证据 |
|---|---|---|
| 形态即认（id / 散文） | `looks_like_indicator_id()`：`X:Y`（尾段 6 位或全大写序列号，支持 `ind:X:all` 多段）**或**登记表查得到 | `domain/agents/decision/gap_queue.py` |
| 基名继承 | `get_by_base()`：登记 id 是本 id 的前缀且紧跟 `:`，**最长者胜** | `infrastructure/catalog/registry.py` |
| 索引补建 | 事实有行、索引无行 ⇒ **补建**（旧实现只 UPDATE ⇒ 新指标永远零行） | `infrastructure/catalog/catalog_repo.py::_refresh_stats_from_facts_sync` |
| 兜底周期 = **月频** | 自动登记无周期 ⇒ `monthly/720h`（原 `daily/24h`） | 同上 |
| 派生计算 | 公式必须带 `sources`；`safe_eval` 只认数字/括号与 `+ - * / // % **`；`to_point()` 的 `period_date` 取**输入的最新期**；缺输入**不产点** | `configs/derived_indicators.yaml`、`domain/indicators/derive.py` |
| 取数器**先 await 完再算** | 在已运行的事件循环里 `run_until_complete` ⇒ 每条都"取不到"（假缺失） | `domain/indicators/fetch_inputs.py` |

### 8.4 闭环与自愈面

| 规则 | 判据/实现 | 证据 |
|---|---|---|
| 批采按**登记频率**聚合 + 月频兜底 | `plan_jobs()`：无 cron 的频率并入 `catalog_monthly`；实测覆盖 84 指标、未覆盖 = 0 | `scheduler/catalog_jobs.py` |
| 维护审计判"**停了**" | `days_since > 3 × 期望周期`（周期**从登记频率读**）+ `expired` 硬截止；先对齐索引与事实 | `scheduler/maintenance.py` |
| 停产从 **registry 现读** | `enabled: false` 的指标不许进报告（索引里的副本会漂移） | 同上（`disabled` 集合） |
| 缺口三路由 | `catalog`（有连接器认它 → 取数侧补）/ `resolver`（无生产者 → A19）/ `prose`（散文 → 只登记不烧钱） | `domain/agents/decision/gap_queue.py::route_gap` |
| 取数侧补采**三态** | 补到新期=`resolved`／**取到但与库内同期 = 源停更的机器证据**／取不到=failed | 同上（`drain_catalog_gaps`） |
| 失败**不放弃** | `retry_later()` 推 30 天且**不消耗** attempts（`skipped` 才是永久） | 同上（`SOURCE_LAG_RETRY_S`、`next_try_ms`） |
| 散文展开 | 逐条复核表；`terminated` 必带证据；`map` 取**并集**；`terminated` 优先于泛化规则 | `domain/agents/decision/prose_map.py` |
| 采集异常只给管理员 | 落登记的 `run_dir`、有界 4000→2000、去重 30 分钟、绝不抛异常 | `core/collection_anomalies.py`、`api/routes/metrics.py` |
| 写权限跟更新责任走 | `warehouse.writer` 一处声明 ⇒ 调度裁剪按 `updates × writable_here()` 派生 | `configs/data_stores.yaml`、`scheduler/registry.py` |

### 8.5 分面清点补充（子代理只读清点）

<!-- 4 个分面清点的详细结果追加在此 -->


---

### 8.8 编排面细节（只读清点，逐条带 `文件:行`）

**① Agent 注册与"18/19/20"三处不一致（照实登记）**

| 来源 | 条数 | 证据 |
|---|---|---|
| `build_runtime()` 注册字典 | **20** | `src/api/runtime.py:364-390`（含 `A19_code_engineer`、`A20_generic_industry`） |
| `configs/agents.yaml` | **19** | 无 A19 条目（A16 → 直接跳 A20） |
| `agent_meta._FALLBACK` | **19** | `src/core/agent_meta.py:22-45` |
| `AGENTS.md` 写的 | **18** | 文档口径 |

⇒ **"18 个 Agent 分 5 层"已经不是事实**（实际注册 20 个），而且 **A19 没有任何层登记**
（只在 `src/domain/platform/llm_cost.py:88` 被映射成 `ops.script`）。这是**文档口径与代码事实的漂移**，
按纪律应以代码为准并改写文档口径。

**② 图结构：21 节点 / 0 条件边 / 扇出三处**

* 节点：`supervisor` · `collect` · `clean` · `validate` · `store` · `verify_info` · `extract_events` ·
  `sentiment` · `liquidity_ctx` · `analyze_{aid}`×**10**（A08/A09/A10/A11/A12/A13/A14/A15/A16/A20）·
  `recommend` · `audit` —— 构建于 `supervisor.py:3798-3867`。
* **条件边 0 条**：`add_conditional_edges` 在该文件**零命中**；所有"分支"都在**节点体内**做守卫
  （`_node()` `:2863-2921`：未注册/不在计划/无输入 ⇒ `return {}`）。
  ⇒ **读这张图不能只看边** —— 真正的分流逻辑在节点体里。
* 扇出：`store → {verify_info, extract_events} → sentiment`（`:3847-3850` join）·
  `store → liquidity_ctx`（`:3851`）· `liquidity_ctx → analyze_*×10 → recommend`（`:3860-3861`）·
  `recommend` 同时等 `sentiment`（`:3863`）→ `audit` → END。

**③ 两处"注释与代码不符"（照实登记，不改代码）**

| 位置 | 注释/docstring 说 | 代码实际 |
|---|---|---|
| `supervisor.py:3852-3858` | 分析层**不再**等 `liquidity_ctx`（"Phase 9 暂回滚：只保留旧路径"） | `:3860` **仍然**是 `liquidity_ctx → analyze_*` ⇒ 分析层仍**串行**在流动性之后 |
| `supervisor.py:2532-2535` | 已拆出 `_build_data_subgraph` / `_build_info_subgraph` / `_build_decision_subgraph`，"约 30 行" | 这三个名字**全仓库只出现在该 docstring 里**（无 `def`）；`build_research_graph` 实长 ≈ **1340 行** |

**④ 规划两条路径的增补顺序（这就是"规则到底按什么顺序改计划"）**

* 分叉点：`supervisor_node` 的 `if llm_plan:` —— `supervisor.py:2939`。
* **规则路径**（`plan_run` `:1617-1698`）：模板 → 拼代码后缀 → 行业路由+产业指标 → 申万估值截面 →
  渗透率（12 组关键词）→ 信息层 Agent 参与 → **流动性补** → **宏观必查项** → 追加 A17/A18。
* **LLM 路径的规划后增补**（`:2948-3042`）：行业名解析 → **后缀处理**（内含 `strip_bogus_code_suffix`）→
  主题新闻词 → **流动性补** → **宏观必查项** → 行业路由裁剪 → **查询信号增补**。
* ⚠️ 两条路径的增补**顺序不同**（规则路径是"流动性→宏观"，LLM 路径是"流动性→宏观→信号"，
  而规则路径的信号增补在 `:3071` 更早）—— 顺序会影响"谁覆盖谁"，属**刻意还是漂移未确认**（诚实登记）。

**⑤ `news` 闸门共 5 处**（`_PLANNING` 空指标 `:1028` · `ensure_macro_indicators` 早退 `:928` ·
`collects_data` 闸 `:1299` · `_apply_query_signal_augmentation` 传参 `:1937` ·
数据管线节点守卫 `:2880`）—— **同一个意图写在 5 处**，本轮新增触发词时差点破掉第 2、3 处。

**⑥ 审计留痕的一处缺口（本轮新发现，未修）**

* LLM 规划那次调用**有**审计（`planner.py:423-460`，`agent_id="supervisor_planner"`）；
* **纯规则规划 `plan_run` 没有任何审计写入**（无 gateway/audit 调用）
  ⇒ **"走了规则规划"与"压根没规划"在审计里长得一样** —— 与 `analysis/base.py::_audit_rule_only`
  当年修掉的那个缺陷**同一形状**，但**规划层还没修**。



> ⚠️ **不要用正则扫 `"indicators": [...]`**：本轮实测 8 个连接器是用**变量**拼的
> （`list(_INDUSTRY_PE)` / `_SERIES` / 族清单），正则会一律报"未声明" ——
> 那是**探针错**，不是代码事实。复跑：`uv run python scripts/_panorama_tables.py`。

| # | 连接器 | source_name | 声明的 indicators |
|---|---|---|---|
| 1 | `a_share_liquidity_connector` | 腾讯财经/东方财富(A股流动性) | `mkt:turnover:total` · `:sh` · `:sz` · `:cyb` · `:kcb` · `:hist` · `mkt:turnover_rate:all_a` · `:hist` |
| 2 | `akshare_connector` | AkShare | `CPI`·`PPI`·`M2`·`社融` · `stock_close:{code}`·`index_close:{code}`·`etf_close:{code}` · `PE(TTM)`/`PB`/`资产负债率`/`流动比率`/`速动比率`/`产权比率`/`ROE`/`ROE加权`/`ROA`/`销售净利率`/`成本费用利润率`/`存货周转率`/`应收账款周转率`/`总资产周转率`/`净利润增长率`/`总资产增长率`/`净资产增长率`/`营收增长率`/`EPS`/`EPS加权`/`每股净资产`/`每股经营现金流`/`每股未分配利润`/`每股资本公积`/`股息率`/`股息率TTM`/`总市值`/`流通市值`/`换手率`/`量比`/`市销率`（均 `:{code}`） · `ind:社会消费品零售总额同比` · `ind:动力煤价格(元/吨)` · **`us_cpi_yoy`·`us_core_cpi`·`us_nonfarm`·`us_unemployment`·`us_fed_rate`·`us_pce`** |
| 3 | `bank_statement_connector` | 新浪财务分析(报表口径) | `利息净收入:{code}` · `利息收入:{code}` · `利息支出:{code}` · `总资产:{code}` |
| 4 | `baostock_connector` | baostock | `stock_close`/`index_close`/`etf_close`（`{code}`） |
| 5 | `coal_inventory_connector` | 中电联CECI周报 | `ind:重点电厂煤炭库存(万吨)` |
| 6 | `compliance_fin_connector` | AkShare合规财务比率 | `商誉占净资产比`/`货币资金占总资产比`/`有息负债占总资产比`/`大股东质押比例`/`对外担保占净资产比`（`{code}`） |
| 7 | `fedwatch_connector` | CME FedWatch | `fed:rate_prob:next` · `fed:rate_prob:{YYYY-MM-DD}` · `fed:policy_range` · `fed:target_upper` · `fed:target_lower` · `fed:effr` |
| 8 | `fred_connector` | FRED(圣路易斯联储) | **（空）** + `supports_template` ⇒ **形态型连接器**：`fred:<SERIES_ID>` 形态即认 |
| 9 | `index_valuation_connector` | AKShare乐咕指数估值 | `idx_val:pe_ttm:{指数名}` · `idx_val:pb:{指数名}` · `idx_val:snapshot:all` |
| 10 | `industry_valuation_connector` | 中证指数官网行业估值 | `ind:消费行业PE(TTM)` · `ind:周期行业PE(TTM)` · `ind:医药行业PE(TTM)`（**只有 3 个行业口径**） |
| 11 | `liquor_price_connector` | 酒价内参(终端零售均价) | `ind:白酒批价(元/瓶)` |
| 12 | `local_csv_connector` | 本地行情CSV（`LOCAL_QUOTE_DIR` 开关） | `stock_close`/`index_close`/`etf_close` |
| 13 | `macro_extra_connector` | 中国宏观PMI/GDP | `PMI` · `PMI:制造业` · `PMI:非制造业` · `GDP` · `GDP:同比` |
| 14 | `margin_trading_connector` | AKShare交易所两融 | `mkt:margin_balance` · `mkt:margin_net_buy` · `mkt:margin_balance:hist` |
| 15 | `northbound_flow_connector` | AKShare东方财富(沪深港通) | `mkt:north_flow` · `mkt:north_flow:hist`（**源已停止披露**） |
| 16 | `penetration_rate_connector` | 渗透率多源采集 | 7 条赛道 + `ind:penetration:{赛道名}` + `ind:penetration_report:{行业关键词}` |
| 17 | `pharma_ind_connector` | CDE药审中心 | `ind:创新药IND申报数量(个)` |
| 18 | `platform_data_connector` | 平台自有后端数据 | 8 族：`估值水位:{code}`·`概念拥挤度:{code}`·`主线告警:{code}`·`个股告警:{code}`·`解禁计划:{code}`·`行业拥挤度:{行业名}`·`板块资金流:{行业名}`·`行业轮动:{行业名}` |
| 19 | `real_industry_connector` | 真实产业数据(WSTS/统计局/中证) | `ind:半导体销售额同比` · `ind:芯片出货量同比` · `ind:科技行业PE(TTM)` |
| 20 | `star_chinext_connector` | 腾讯/东财/乐咕(双创) | 9 条**相对键**（内部前缀 `mkt:cybkcb:`）：`spot_summary`·`turnover:all/cyb/kcb/kcb_all`·`turnover_hist`·`val:all/cyb_pe/kcb_pe` |
| 21 | `sw_industry_valuation_connector` | AKShare申万行业估值 | `ind:sw_first/second/third_pe_ttm`·`_pb`（`:all` 与 `:{行业名}` 两形态）+ `ind:sw_third_dividend_yield:{行业名}` |
| 22 | `tencent_daily_connector` | 腾讯财经日K | `stock_close`/`index_close`/`etf_close` |
| 23 | `tushare_connector` | Tushare Pro（**消耗积分**） | `stock_close`/`index_close`/`etf_close` |
| 24 | `xtquant_connector` | 迅投QMT（**默认关闭**，排链尾） | `stock_close`/`index_close`/`etf_close` |

**读这张表要带的两条约定**（否则会误判成缺口）：

1. **相对键是约定**，不是漏写命名空间 —— `star_chinext` 的 9 条在代码里会被加上
   `mkt:cybkcb:` 前缀（`star_chinext_connector.py` 的 `_INDICATOR_RE_PREFIX`），
   `tests/unit/test_contract_consistency.py:226` 专门写着"拿相对键做'是否已登记'的比较是误报"；
2. **能力表 ≠ 登记表**：能力表说"我取得到"，登记表说"平台有这个指标"。
   两者不一致时**以登记表为准**（计划只从登记表来）。

**这张表当场暴露的两条设计不足（本轮新增）**：

| # | 不足 | 证据 | 级 | 说明 |
|---|---|---|---|---|
| 11 | **"停产"只落在登记面，没落在能力面** | `akshare_connector` 的 `get_capabilities()` 里**仍列着** `us_cpi_yoy`/`us_core_cpi`/`us_nonfarm`/`us_unemployment`/`us_fed_rate`/`us_pce`，而 registry 已把它们标 `enabled: false` | B | 后果：`supports("us_unemployment")` 仍为真 ⇒ **若有人按老名字请求**（散文映射撞上、或外部脚本直连连接器），拿到的会是**一年多前的陈旧值且不报错**。修法：能力面也标停用（让 `supports()` 返回假），或明确"停产 = 计划不排但历史可查"并加一条判据 |
| 12 | **形态型连接器在能力枚举里不可见** | `fred_connector` 的 `indicators` 是**空表**，靠 `supports_template` 与形态判据取数 | B | 后果：任何"**按能力枚举候选源**"的机制（含 §7.2 的换源提案）**看不到 FRED 的任何序列** ⇒ 提案必须为"形态型源"单独设计枚举方式，否则会漏掉最有用的一类通用源 |

### 8.7 调度作业穷举表（48 个）

`JOB_REGISTRY` 现读（`scripts/_panorama_tables.py`）。按 `kind` 分布：
`catalog_collection×4` · `event_alert_scan×3` · `graph_snapshot×2` · `strategy_cases_fetch×2` ·
`generic_indicator_snapshot×2` · `tech_industry_snapshot×2` · `quant_select×2` · 其余 26 种各 ×1
（含 `data_freshness_audit` · `gap_drain` · `derived_metrics` · `calendar_sync` · `daily_warm` ·
`unlock_plan` · `crowding_metrics` · `quant_data_sync` · `mainline_*` · `intel_*` · `data_retention` · …）。

**数据更新类作业的 cron（这是"数据多久刷一次"的唯一真值源）**：

| 作业 | cron | 覆盖 |
|---|---|---|
| `catalog_daily` | `30 16 * * 1-5` | 20 指标 |
| `catalog_weekly` | `40 16 * * 5` | 3 指标 |
| `catalog_monthly` | `0 9 3 * *` | 30 指标（含 **9 条月频兜底**） |
| `catalog_quarterly` | `0 9 5 1,4,7,10 *` | 31 指标 |
| `catalog_calendar` | `20 7 * * 1-5` | 日历类（解禁/财报/宏观日程） |
| `data_freshness_audit` | `45 7 * * *` | 全量登记指标的"停了才算"审计 |
| `gap_drain` | `0 22 * * 1-5` | 缺口三阶段（散文展开→取数侧补采→A19） |
| `derived_metrics_monthly` | `10 8 6 * *` | 派生指标落库（攒趋势） |
| `unlock_plan_monthly` | 每月 1 日 08:20 | 逐股解禁表 |
| `quant_data_sync` | — | 行情仓（**写权限：pilot**） |

**读法**：`catalog_*` 四个作业是**按登记频率自动聚合**出来的（`plan_jobs()`），
**不要手写这份名单** —— 加一条登记指标，作业覆盖自动跟着变。

---

### 8.10 四个只读分面清点查出的**新问题**（本轮新增，逐条带证据）

> 这一节是四个分面清点（编排 / 采集 / 匹配计算 / 闭环自愈）里**我基线之外**的新发现。
> 每条都给了 `文件:行`；**未修的都如实留着**，不假装已解决。

| # | 问题 | 证据 | 影响 | 状态 |
|---|---|---|---|---|
| 1 | **`gap_drain` 作业从未成功跑过** —— 运行台账 dev/pilot 各 2 条**全部 failed**，错误正是 `cannot access local variable 'data_repo'`（`del data_repo` 写在循环内那版） | `data/dev/scheduler/runs.jsonl`、`data/pilot/scheduler/runs.jsonl`（末次 dev `2026-09-29T22:00:02`，30.4s）；源码已修（`del` 移出循环） | **"缺口补采"这条闭环在真实调度里一次都没成功过** | **源码已修**；cron 22:00 未到，**待验** |
| 2 | **A19 自愈产物的定时作业恢复不到** —— `data/dynamic_connectors/` 有 5 个 `gap_*.py` 落盘，但 **`_schedule.json` 不存在** ⇒ `load_dynamic_jobs()` 恢复 0 条 | `registry.py:860-878`（恢复点 `runtime.py:334-335`）、路径 `registry.py:822-823` | 自愈生成过的连接器**只活在路由器热加载层**，没有定时采集 ⇒ 下次没人问就又会缺 | **未修**（建议：文件缺失必须 warning，或从 `gap_*.py` 反推重建） |
| 3 | **`JobKind` Literal 与实际使用的 kind 不一致** —— 实际 36 种，其中 **6 种不在 Literal 里**（`generic_indicator_snapshot`/`mainline_daily`/`mainline_etf_flow`/`mainline_calibrate`/`intel_hot_topics`/`intel_signal_alert`） | `registry.py:21-49` vs `:252/265/561/579/640/766/805` | Literal **运行时不校验** ⇒ 不报错、也没人发现（"设了不生效"的形状） | **未修**（建议：从 `JOB_REGISTRY` 派生 + 一条断言） |
| 4 | **`NEEDS_MAINTENANCE` 里的 `lagging` 永不产出** | `maintenance.py:71`、`:361` vs `_classify` `:144-191` | 常量在说谎（"这条判断存在"其实没有） | **未修** |
| 5 | **定期维护作业没有 cron 触发记录** —— dev 2 条都是 `trigger="probe"`，pilot/主实例 **0 条** | 两个 `runs.jsonl` | 07:45 那一班**是否真跑过无证据** | **待验**（明天 07:45 后复查） |
| 6 | **`configs/data_stores.yaml` 未被 git 跟踪**（`??` 状态） | `git status --porcelain configs/data_stores.yaml` | 存储**单一真值源不随仓库发布** ⇒ 克隆环境没有它（与 `scripts/` 那类同形） | **未修**（建议进白名单） |
| 7 | **`AkshareConnector` 声明 48 条里 2 条重复**（`资产负债率:{code}`/`流动比率:{code}` 硬编码 + 派生表各一份），去重后 46 | `akshare_connector.py:701-711` vs `:396-397` | 只影响计数（`_matched` 走 `supports`，不按声明去重） | **未修**（低危备查） |
| 8 | **`should_display` 注释与实现不一致** —— 注释写「（>0.1）」，实现是"未超硬截止即 True"（`conf=0.001` 也 True） | `data_freshness.py:123` vs `:209` | 读码的人以为有 0.1 的闸，其实真正的闸是 `filter_days` | **未修**（改注释或改实现，二选一） |

**联网兜底的"生效配置"是环境事实，不是仓库默认**：仓库默认白名单**空（全禁）**
（`network_fallback.py:150`），而**本机 `.env:49` 显式开了 6 个源**（= `SUGGESTED_ALLOWLIST`）。
⇒ "联网兜底能用"是**本机配置**的结果，**克隆环境默认是关的**；写文档/排障别把本机行为当默认。

### 8.12 匹配与计算面清点查出的新问题（本轮新增）

| # | 问题 | 证据 | 影响 | 状态 |
|---|---|---|---|---|
| 1 | **`ENQUEUEABLE_CODES` 零生产消费者** —— "只有 `NO_DATA`/`NO_TABLE` 才配进补采队列"这条规则**不被任何代码执行**（`src/` 内零读取；真实入队点有 4 处，各按自己的理由入队） | `local_data.py:162-163`（定义）vs 入队点 `supervisor.py:3755-3789`、`jobs.py:1804/1989-1994`、`maintenance.py:278-282`、`prose_map.py:322-333` | **一条声明式闸门**：读代码的人以为入队被约束，实际没有 | **未修**（要么接上，要么删掉并说明） |
| 2 | **A12 白名单缺 `解禁计划:{code}`** —— A12 只放行 `cal:`（三条聚合），个股级解禁进不去 | `supervisor.py:295-322`（A12 块）vs `:281`（A11 有 `解禁计划`） | 若期望 A12 看个股解禁 ⇒ 又一次"数据到了 Agent 看不见" | **未修**（需先确认 A12 是否该看） |
| 3 | **注释里的判据规模已漂移** —— 注释写"目录 **40** 条 vs 集合 **24** 个"，现算为 **39 vs 18** | `supervisor.py:699-700`（注释）vs `_CATALOG_CODE_SUFFIX_ROOTS` / `_CODE_SUFFIX_INDICATORS` 现算 | 判据本身是派生的（不会错），**漂的是注释** ⇒ 读者会低估覆盖差 | **未修**（注释改写） |
| 4 | **两个"陈旧"阈值不是同一个数** —— 展示容忍 `DEFAULT_STALE_DAYS = 400` vs 补采触发 `STALE_TRIGGER_DAYS = 30` | `local_data.py:279` vs `network_fallback.py:315` | 不是缺陷（刻意），但排障时容易把两者当成一个 | **登记备查** |

**计算面的关键阈值（照抄，便于评审）**：A10 分位样本下限 **8**（`micro/agent.py:37`）· A11 解禁阈值 **500 亿 / 100 亿 / 20 家**（`risk/agent.py:21-23`）· A12 六族规则阈值 **关联交易 30 / 商誉 30 / 质押 80·50 / 担保 100·50**，存贷双高 **40+40 同时超**（`compliance/logic.py:73-88`）· A08 利率档 **≥4.5 鹰派 / ≥3.5 中性 / 否则鸽派**，美林时钟 **CPI≥3.0 且失业率≤4.5=过热**（`macro/agent.py:316-324`、`:370-378`）· 幻觉护栏三层（数字溯源 / 代码 grounding / 来源标注，默认关）（`hallucination_guard.py:235-273`）· `extra` 里现算 **224 个不同口径键**。



### 8.11 采集面的关键口径（只读清点补充）

| 项 | 值 | 证据 |
|---|---|---|
| 需要 API key 的连接器 | **只有 1 个**：`TushareConnector`（`TUSHARE_TOKEN`）—— 也因此被排除在联网兜底白名单外（唯一花钱源） | `tushare_connector.py:300`、`network_fallback.py:189-203` |
| 本机实际生效连接器 | **22 条**（静态 24 − `LocalCsv`（`LOCAL_QUOTE_DIR` 空）− `QMT`（`QMT_ENABLED=0`））+ 动态热加载 | `runtime.py:221`、`:230`、`.env:4/8` |
| TTL 缓存 | **按前缀**，首个 `startswith` 命中即返回（更具体的前缀必须排前面）；月频宏观 24h / 估值 12h / `fed:` 1h / 日线 4h / 双创截面 5min | `router.py:46-79`、`:368-373` |
| 失败冷却 | 默认 **300s**（按 `连接器×指标`）；超时**不记**冷却 | `router.py:304`、`:759-761` |
| `stale_skip` | **90s** 短期记忆（源给不出更新期时先跳过该跳，但把旧数据放进候选） | `router.py:126`、`:711-728` |
| `deadline_sec` | **整条指标的墙钟预算**（不是单跳） | `router.py:678-682` |
| 联网兜底闸门顺序 | 白名单 → 账本可用 → 源允许 → 冷却 → 日预算 → 小时频率（**检查与记账合一，关 TOCTOU**）；账本读不到 ⇒ **fail-closed** | `network_fallback.py:932-1027`、`:1056-1069` |
| 诊断码对齐 | 允许 **5** + 禁止 **12** = **17** = `DiagCode` 全集；**未知码 ⇒ 不联网**（"未知即不许花钱"） | `network_fallback.py:271-305`、`:1528-1529` |
| 兜底熔断档位 | `fallback:<源名>` **不在** `_DEFAULTS` ⇒ 落到 `deepseek` 档（3 次/60s 窗、恢复 30s） | `circuit_breaker.py:135-141`、`:155` |
| A04 幂等键 | **`UNIQUE(indicator, period_date, raw_content_hash)`** + `INSERT OR IGNORE`（派生点的 hash 由 `indicator+extra` 算 ⇒ 同输入复跑幂等） | `macro_repo.py:55`、`:61-64`、`schemas.py:131` |
| 平台自有族 | **8 族**，口径字段随 `extra` 下发（`估值水位` 13 个原始列；`解禁计划` 是**唯一**会产 `value=0.0`（"量到 0"）的族） | `platform_data_connector.py:195-231`、`:2040-2080` |



**采集路径的「联网兜底」从未执行** —— 用户口径「找不到就去**联网搜索找**」在采集侧**等于没接**：

* `_fallback_fetch(indicator, *, agents, context, trigger)` 的签名里**没有 `state`**
  （`supervisor.py:2311-2313`）；
* 采集路径唯一调用方写成 `_fallback_fetch(indicator, state=state, agents=…, …)`
  ⇒ 每次调用抛 `TypeError: unexpected keyword argument 'state'`；
* 该异常被 `except Exception` 吞掉，且**只记 `logger.debug`**（默认不可见）
  ⇒ 返回 `[]`（"没找到"）⇒ **看起来一切正常**；
* `grep -r "_network_lookup_for_collection|LOCAL_MISS" tests/` **零命中** —— 这条路径**没有护栏**。

这正是 `AGENTS.md` 记过的"`NetworkFallback` 零生产调用方"的**同一形状复发**。

**已修（三处，都在 `supervisor.py`）**：① 调用点去掉 `state=state`；
② 那条宽 `except` 的日志 **`debug` → `warning`**（兜底坏了必须看得见）；
③ 新增护栏 `tests/unit/test_collection_network_fallback_wiring.py`（**6 条**）——
逐调用点用 **AST** 核对关键字与签名一致（**编译器抓不到、运行期又被吞**，只能靠判据）·
按调用点原样关键字真调一次（空 agents ⇒ 不触网）· 断言异常是 `warning` 不是 `debug` ·
反向判据："宽 except 不许被删"（联网是最后一层，坏了不能拖垮采集）。
**自证**：把老代码那段喂给判据 ⇒ 报出 `state`；喂修好的 ⇒ 不报。

---

## 九、复跑命令


```bash
uv run python scripts/_panorama_facts.py          # 本文档 §二 全部硬数字
uv run python scripts/prd_sync_check.py --ledger  # 需求—PRD 台账完整性（交付前 0 ERROR）
```
