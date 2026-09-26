# 面试复盘：ETF 回测统计口径与板块剔除（本会话沉淀）

> 范围声明：本文**只覆盖本次会话内实际发生的工作**——ETF 份额监控根因诊断、
> ETF 回测统计口径改造（独立样本量 / Wilson 区间 / 样本内外对照）、资金流「全显」双向开关、
> 板块资金流榜剔除清单，以及**AI/Agent 编排层架构地图**。
>
> **与仓库内其它面试文档的关系（不要重复讲）**：
> - `INTERVIEW_PLAYBOOK.md` —— 量化交易主链路（竞价/量化选股/做T/日K/回测/资金流/拥挤度/数据层）。
>   本次资金流与板块剔除属其 §2.6，那边只加了一行指针。
> - `INTERVIEW_会话总结_量化与AI双岗.md:122-129` —— **LLM 基础设施**（多 provider 链、tier 路由、
>   缓存、熔断、审计、成本）已成体系，本文**不重复**，只在 §5 补本次复核到的代码级细节。
> - `INTERVIEW_NOTES.md` —— 语料 → 技能 → 因子（另一次会话）。
> - 本文的**独有增量**是 §5 **Agent 编排层**架构地图：全仓库此前没有任何一篇面试文档
>   写过 `BaseAgent` 契约、StateGraph 拓扑、ReAct 护栏（grep `BaseAgent` 在 `docs/` 零命中）。
> - 数据质量细节（T+1 时序根因链条）见 `docs/ETF_FLOW_DATA_QUALITY.md`，本文只留结论与面试讲法。
>
> ⚠️ 三处红线写在 §6：**没有 MCP**、**没有原生 function calling**、**样本外只有 1 个独立时段**。

---

> 本次会话做了 5 件事，其中 2 件是**纯诊断**（没改代码）。
> 每一条都带 `文件:行号`，方便回去读原文——**这些行号是要你亲自去读的，不要只背结论**。
>
> ⚠️ 最值钱的是**第 3 节（踩坑）** 与 **第 6 节（诚实边界）**，不是功能清单。

## 1. 一句话定位（本部分的 3 分钟自述骨架）

> 这次我做的是**把一套已有回测的口径讲清楚，并补上它缺失的可信度指标**：
> 原报告给出"机会信号胜率 83.3%、T+34 中位数 +9.18%"，我把样本独立性算进去之后发现
> ——**66 条信号只对应 5 个独立事件，样本外只剩 1 个**，于是把结论从"有效"改成"未验证"，
> 并把这套判断做成了回测报告的常驻列（独立时段 + Wilson 区间 + 样本内外对照）。
> 另外两块是资金流模块的工程活：一个前端开关的状态判据修正、一份 123 条板块剔除清单的落地。
>
> 三条主线：**口径 → 可验证性 → 静默失效**。

## 2. 功能清单（带可验证数字 + 代码位置）

### 2.1 ETF 份额监控「核心宽基份额整块没数据」根因定位（纯诊断，0 行改动）

**现象**：面板上 9 只核心宽基 ETF 的份额与 1/5/10/20 日变化**全部**显示 "—"。

**根因链条**（这条是"数据发布时序 vs 业务口径"的完整案例）：

| 步 | 事实 | 代码位置 |
|---|---|---|
| 1 | 份额来自 `fund_share.fd_share`，**次日 8:30** 才发布；价格来自 `fund_daily`，当晚就有 | `src/mainline/datastore.py:216` |
| 2 | 每晚 17:30 全市场同步**只同步最新一个交易日**，必然写入一批 `shares=NULL` 的行 | `src/mainline/datastore.py:1890-1900` |
| 3 | 面板锚点是 `SELECT MAX(trade_date) FROM ml_etf` | `src/mainline/etf_flow.py:871` |
| 4 | 每只 ETF **只读最后一行** | `src/mainline/etf_flow.py:468` |
| 5 | 该行 `shares=None` → `change_1d/5d/10d/20d` 全走 `_pct_change(None,…)` → `None` → **五列一起变 "—"** | `src/mainline/etf_flow.py:475-480` |
| 6 | 已有的"NULL 不覆盖旧值"保护**无效**：那些行是当晚**新插入**的，没有旧值可保护 | `src/mainline/datastore.py:1978-1981` |

**关键洞察**：锚点是**一个全局日期**，不是逐只各取各自最后一天 → 所以现象是"**都没数据**"而不是"部分没数据"。**这一条是面试的得分点**：能从"整块空白"反推出"锚点是全局的"。

**实测数字**：2026-09-21 17:55 那次同步落 2138 行、只有 765 行有份额，9 只监控宽基里 **7 只**中招。
全表 377/2138 缺份额是**正常**的（`160xxx`/`501xxx` 这类 LOF/场外基金份额长期不发布，不在监控清单内）。

**我另外查出的两件事**（讲"现有修复还不完整"）：
1. `ml_sync` 台账里**没有任何 `etf_share` 记录** → 那条"启动自检补拉"路径（`src/mainline/etf_share_guard.py`，挂在 `src/api/main.py:249` 与 `src/scheduler/jobs.py:795`）**在生产上从未成功执行过一次**，只有单测覆盖。
2. 三个残留缺口：① 傍晚到次日 8:30 面板必然空白且**不会回退到上一个份额齐全的交易日**；② 「立即刷新」是只读路径、**不触发自检**；③ 只有"最新一天"会被回补，更早的 NULL 永不回补。

**修复路径的设计（不是这次做的，但要能讲）**：`datastore.sync_etf_shares()` 用 `UPDATE … WHERE shares IS NULL` —— **幂等、只补不造**，绝不插入"有行情没份额"的新行（那正是当初出事的机制，见 `datastore.py:1991-2002` 的 docstring）。

### 2.2 ETF 回测四张表的口径（分析）

四张表**同源**（同一个 `report.records`，只有筛选字段不同，见 `src/mainline/etf_flow_backtest.py` 的 `_aggregate()`）：

| 表 | 键 | 回答什么 | 能不能定门控 |
|---|---|---|---|
| 分类型胜率 | `opportunity`/`risk`/`industry_reversal` + `level:*` + `*_live`/`*_gated` | 哪类信号方向有效 | **`*_live` vs `*_gated` 的差值就是门控价值** |
| 分市场环境胜率 | `bull`/`bear`/`range` | 整体可靠性 | ❌ 三类混在一起会互相抵消 |
| **分类型×环境胜率** | `kind@regime` | — | ✅ **门控规则的唯一依据** |
| 分ETF产品胜率 | ETF 代码 | 单产品异常 | ❌ 样本 49~419，同系列同日触发、不独立 |

**要点**：`live` 桶按**等级**判定而不是只看 `gated=false`——观察列表里的弱信号 `gated` 也是 false 却从不告警，混进去会把放行桶稀释掉（代码注释记了第一版踩过的坑：live 桶 359 样本 vs 熊市实际 77）。

### 2.3 补上可信度指标（本次主要实现）

**新增 `src/mainline/etf_flow_stats.py`**：

| 函数 | 作用 | 关键设计 |
|---|---|---|
| `independent_episodes()` | 按观察期长度做贪婪去重，数出**前瞻窗口互不重叠**的时段数 | 基准取"上一个**被接受**的日期"而非"上一个原始日期"；按**交易日**而非自然日算间隔 |
| `wilson_interval()` | 胜率的 Wilson 95% 区间 | 小样本 + 比例接近 0/1 时正态近似会跑出 [0,1] 之外，Wilson 天然有界 |
| `resolve_targets()` | 按观察期取目标线 | **取不到返回 None（不表态）**，绝不拿别期的线顶上 |
| `target_hit()` | 按**信号方向**判达标 | 做多看 `median ≥ +目标`，风险类看 `median ≤ −目标` |

**改造 `src/mainline/etf_flow_backtest.py`**：`HorizonStats` 加 `independent` / `win_rate_low` / `win_rate_high` / `kind`；`_aggregate()` 增加 `calendar` 参数与样本内外对照；`run()` 读 `backtest.split`；`render()` 新增列与「六、样本内/样本外对照」章节。

**配置 `configs/etf_flow.yaml`**：新增 `backtest.split: "20230101"`；`targets` 由"所有观察期共用一把尺子"改为**按观察期分别设线**。

**前端**：`web/src/etfFlowApi.ts`（类型）、`web/src/components/EtfFlowBacktest.tsx`（新增「独立」列 + 胜率挂 Wilson 区间 + 新增样本内外对照卡，`≤2` 时标 ⚠️）。

**实测结果 —— 这组数字必须背下来**：

| 分组 | 样本 | **独立时段** | T+34 中位数 | 胜率 | Wilson 95% |
|---|---:|---:|---:|---:|---|
| 机会信号（放行）**全区间** | 66 | **5** | +9.18% | 83.33% | [72.6%, 90.4%] |
| 机会信号（放行）**样本外** | 37 | **1** | +9.42% | 100% | [90.6%, 100%] |
| 风险信号（放行）全区间 | 247 | **32** | −2.00% | 30.36% | [25.0%, 36.4%] |

**这张表本身就是最好的面试素材**：37 个样本、100% 胜率、区间下界 90.6% —— 看着像重大发现，**实际只有 1 个独立事件（2024Q1）**。同一格 T+10 已经翻成 45.95%。

> ⚠️ **一个必须自己讲的修正**：我第一版算出的"3 个独立时段"是**错的**——当时用"信号日集合"当伪日历算交易日间隔，把距离放大了。改用真实主指数日历后是 **5**。
> 这个"自己发现并修正"的过程比数字本身更能证明研究素养，**建议主动讲**。

### 2.4 资金流走势「全显」改一键开关

同一个按钮按状态切换：全部可见 → 显示「全不显示」；全部隐藏 → 显示「全显」。
代码位置：`web/src/components/FundFlowBoard.tsx`（`allHidden` 判据 + 按钮）、`web/src/components/FundFlowChart.tsx`（空状态文案）。

两个正确性细节（都能当 React 面试题）：
1. **判据必须按 `chartEntities.every(...)` 算，不能用 `hidden.size`**：`hidden` 只在两处写入、**从不随选择变化清理**，所以"隐藏几条 → 清空选择 → 另选几条"之后 `hidden.size > 0` 但实际一条都没隐藏 —— 按钮会显示成「全显」而**点下去什么都不会变**。
2. **先确认过 `ready = lines.length > 0 && domain !== null` 会拦住"全部隐藏"**（`FundFlowChart.tsx:271`），否则 `domain.span` 会在 SVG 里抛异常把页面点白。

### 2.5 板块资金流榜剔除非行业/概念板块

**最关键的一条发现**：用户口径的「882 开头」**在这个功能里不存在**。

| 体系 | 代码形态 | 例 |
|---|---|---|
| 同花顺（用户口径 / `ml_board` 池） | `882xxx.TI` 等 | 882/883 已按用户口径**不进池**，实测 `ml_board` 只有 885/886 的 161 个 |
| **东财 `moneyflow_ind_dc`（资金流榜的数据源）** | `BK0159.DC` | 江苏板块=`BK0159.DC`，富时罗素=`BK0867.DC`，融资融券=`BK0596.DC`，华为概念=`BK0854.DC` |

**两套体系零重叠 → 地域性只能按名称识别**。另外榜单层拿到的 `code` 其实等于 `name`（真代码在底层截面里，见 `src/fundflow/service.py` 的 `_rank_sectors`）。

**产出**：
- `configs/fundflow_sector_exclude.yaml`（**123 条冻结清单**，六类：地域 37 / 海外指数与指数成分 19 / 资金持仓属性 9 / 风格规模 22 / 市场统计情绪 35 / 用户点名 1）
- `src/fundflow/sector_filter.py`（加载 / 代码+名称双匹配 / `filter_payload` / `filter_names` / `disclosure` / **`verify_codes`**）
- 接在**三个位置**：`src/fundflow/service.py` 的 `_build()`（**必须在 `ensure_defaults` 之前**）、过滤已选列表、`src/api/routes/fundflow.py` 的搜索

**效果**：1030 个板块剔除 123、保留 907。默认热门前 20 里 **14 个**是聚合板（融资融券 −302 亿、深股通 −154 亿、富时罗素 −145 亿…），全被换成真实行业板。

## 3. 踩坑集（本部分，按"现象 → 根因 → 怎么讲"）

### 坑 1 ⭐⭐⭐ 静默失效：关键词匹配会误伤正常板块

- **现象**：用宽口径关键词扫 1030 个板块筛"非行业板"，结果打中 `汽车一体化压铸`（含"一体化"）和 `重组蛋白`（含"重组"）—— 两个都是**明确行业**。
- **根因**：中文板块名里"看起来像统计词"的子串，在别的语境里是行业术语。被误删的板块**不报任何错**，榜单上只是"少了两个"。
- **修法**：最终**全部用显式清单**，不做关键词规则；`汽车一体化压铸`/`重组蛋白` 连同"为什么保留"写进配置的 `considered_kept` 段。
- **怎么讲**："我最初想用规则，跑一遍就发现规则会误伤——**静默删掉一个真行业板块，比漏掉一个统计板危险得多**。所以我改成显式冻结清单 + 把'考虑过但保留'的判断也写进配置，让下一个人不用重新纠结。"

### 坑 2 ⭐⭐⭐ 我差点按序号「猜」代码，会把真行业板块删掉

- **现象**：补录 11 个板块时，我按序号**猜**了代码：`BK0486.DC` 想指"标准普尔"、`BK1622.DC` 想指"科创板做市商"、`BK1623.DC` 想指"科创板做市股"。
- **真相**：这几个代码实际属于 **传媒**、**镍**、**钼**。（另 `BK0505.DC` 是**中字头**、`BK0965.DC` 是**社区团购**。）
- **根因**：匹配同时按**名称**和**代码**两条路。名称匹配不上、代码却命中 → 被删的是"另一个板块"，而且**两条路看起来都正常**。
- **修法**：全部改为从数据源实际值抄；并新增 `verify_codes()` —— 每次组装都校验全部 123 条 `(code, name)` 配对，不一致就写进界面 `source_notes`（`src/fundflow/service.py` 的 `_build()` 里调用）。
- ⚠️ **这条不是首创，讲的时候必须交叉引用**：`docs/MAINLINE_MINING.md:5080-5095`（§16.68 二）已经记录过**同类事故**——拥挤度模块手写 `.TI` 代码，"食品添加剂 → 885406.TI（真实是食品安全）"、"商品服务与用品 → 875201.TI（真实是元宇宙）"，4/6 个手打代码是错的，修法是"**名称→代码的解析全部走库，代码一个字都不手写，并加名称回读断言**"。
  本次是**同一个错误在另一个模块重演**（拥挤度用同花顺 `.TI`，资金流榜用东财 `BK`）。
  → **正确讲法**："这是我在这个项目里**第二次**遇到同一类问题——第一次在拥挤度模块（`MAINLINE_MINING.md` 有记录），结论是'代码不许手写、要走库解析 + 回读断言'，但那条结论**没有被沉淀成通用机制**，所以我在资金流榜上又踩了一次。我这次补的 `verify_codes()` 就是把它变成**机制**而不是纪律。"
  → 讲成"我首创"被追问就崩；讲成"同一坑踩两次、第二次我把它机制化了"**反而是加分项**。
- **怎么讲**："这是我这轮最危险的一处。**双键匹配的代价是：写错一个键会静默删掉另一个实体。** 所以我加了一道自校验，把'清单与数据源不一致'变成界面上可见的错误，而不是让它在数据里烂掉。"（校验结果：123 条全一致、0 不一致、剩余 907 个板块 0 个漏网。）

### 坑 3 ⭐⭐ 关键词扫描**自己**会漏（第一轮漏了 14 个）

- **现象**：第一轮关键词扫描漏掉 `中证500`/`深成500`/`创业板综`/`上证380`/`深证100R`（**不带 `_` 后缀**，而规则是按 `HS300_` 那种形状写的）、`标准普尔`（我写的是"标普"，名字里其实是"**普尔**"）、`百元股`/`趋势股`/`破发股`/`题材股`/`东方财富热股`/`科创板做市商`/`科创板做市股`/`GDR`。
- **修法**：改成**扫两遍** —— 一遍扫全集，一遍扫**剔除后的残渣**；命中项逐个过目；共三轮补齐。
- **怎么讲**："**只扫一遍必漏**。第一轮我漏了 14 个，其中 5 个是同一类形状（指数成分板）——说明我的扫描规则本身有盲区。第二轮我改成对'剔除后剩下的 910 个'再扫一遍，才补全。"

### 坑 4 ⭐⭐ 过滤点的顺序错了会污染持久化数据

- **现象**：默认热门是**按当日净额绝对值**取前 20 播种到用户的选择列表（`src/fundflow/service.py` 的 `ensure_defaults`）。而融资融券单日 −302 亿，**必然排第一**。
- **根因**：如果先播种再过滤，聚合板会被**写进用户的持久化列表**；之后再滤，界面上就只剩"它在监控列表里，却永远不进榜单"的尴尬状态。
- **修法**：过滤**必须在 `ensure_defaults` 之前**。
- **附带发现**：用户库里**早就躺着** `MSCI中国`/`基金重仓`/`大盘成长` 三条（清单是后加的）→ 处理方式是**只跳过、不删除**（那是用户的选择，删了不可逆），并如实报"跳过了几个"，否则"选了 12 个只画出 9 条线"会像 bug。

### 坑 5 ⭐⭐ 冗余指标 + 一把尺子量所有观察期

- **现象 A**：`targets.median_return_34: 0.045` 被**所有观察期共用** → T+5 那一行拿 5 日收益中位数去比 34 日的 4.5%，**永远"未达标"**，那个 ⚠️ 没有信息量。
- **现象 B**：假阳性率的定义是"收益 ≤ 0 的比例"，它**恒等于 `1 − 胜率`** → 给它再设一条门槛等于把同一个条件写两遍，而且"未达标"会有两个说不清的原因。
- **修法**：目标线按观察期分别配；`false_positive_rate` 从配置里删掉并在文档写明恒等关系；胜率加 Wilson 区间。
- **怎么讲**："这两条都是**报表在骗人**：一个永远亮红、一个永远重复。修法不是调阈值，是先把口径修对。"

### 坑 6 ⭐⭐ 达标判定不分方向 = 结构性永久失败

- **现象**：风险信号（高位 + 份额大减 → 预测下跌）用"中位数 ≥ +4.5%"判达标 → **一个判断正确的看空信号在报表里永远显示失败**。
- **修法**：`target_hit()` 按信号方向取不等式；风险类胜率门槛也**镜像**（要求 `≤ 1−目标`，即"大多数时候确实在跌"）。
- **⚠️ 但不能顺势放宽门槛**：修正后风险信号**仍然未达标**（−2.00% 达不到 4.5% 的**对称**门槛）。这是"方向对、幅度不够"的**真实结论**，我刻意没有替它编一个更低的门槛 —— 那就是对着数据凑参数。
- **怎么讲**：这个"改了口径但结论没变好、我也没去粉饰"的处理，比改对本身更能说明问题。

### 坑 7 ⭐⭐ 并发编辑：别人正在改同一批文件

- **现象**：施工期间有另一处编辑在改**同一批文件**（加了明细去重 `collapse_signal_runs`、把「达标」列从四张表里删掉、把 `signals` 补进 `_etf_backtest_dict`）。
- **处理**：先读现状，再**只动他们没碰的地方**，用精确匹配的编辑而不是整体覆写；他们的设计决定（删「达标」列）**予以保留**。
- **怎么讲**："我发现有并行修改后，没有覆盖，而是按'互补'来做——**他们的设计决定我不推翻，我的改动放在他们没触及的层**（统计口径 vs 展示层）。"

### 坑 8 ⭐ 构建成功 ≠ 线上生效

- **做法**：前端改完后，我把**服务返回的字节流**与本地构建产物做 **SHA256 比对**（`4179449c8228de67`，681870 字节，完全一致），并确认服务托管的 `index.html` 已引用新 bundle。
- **顺带一条排障经验**：用 PowerShell `Invoke-WebRequest` 抓 JS 后 `Contains("中文")` 返回 False —— 那是**单字节解码假象**，不是真的没有；改用 Python 按 UTF-8 解码后全部命中。
- **怎么讲**："我不接受'构建成功'作为验收，**要证明线上真的在跑新代码**。"

## 4. 面试高频问题 + 建议答法

### 4.1 量化开发岗

| 问题 | 建议答法 | 陷阱 |
|---|---|---|
| 83% 胜率，有效样本多少？ | **全区间 5 个独立时段；样本外 1 个** | 只背 83% 就完了。主动说"未验证" |
| 你怎么定义"独立"？ | 前瞻窗口互不重叠：按观察期长度对信号日做贪婪去重，**按交易日**而非自然日算间隔 | 要能说出"基准取上一个**被接受**的日期"这个细节 |
| 为什么用中位数不用均值？ | 收益厚尾，均值被极端值主导 | 别只说"更稳健" |
| T 和 T+1 口径差在哪？ | 份额次日 8:30 才发布 → T 口径**实盘拿不到**；差异大 = 收益来自信号日当天已涨完的部分 | 要能说出 T+1 是 **N−1 天**持有 |
| 样本内外怎么切的？ | `split: 20230101`，2018–2022 定阈值、2023 起验证 | 必须承认"阈值是全区间调的" |
| 多重检验怎么处理？ | 坦白：**没有**。我在 kind × regime × horizon × level × 阈值上看了很多格子 | 别装作做过 |
| 未来函数怎么排除？ | 回测按日切片，`classify_regime(closes[:index+1])` 只用当日及之前 | 这是代码事实，可讲 |
| Wilson 为什么不用正态近似？ | 小样本 + 比例接近 0/1 时正态近似会跑出 [0,1] 外 | 能写公式更好 |
| 收益基准为什么是指数？ | 折溢价/摩擦未纳入，衡量的是"判断力" | 文档已写明这条局限，主动提 |
| 这能实盘吗？ | **低频环境温度计**（门控后年均约 8 次），不是可下注的系统优势 | 说"能"就露馅 |
| 重做会怎么改？ | ① 报有效样本量（已做）② walk-forward ③ bootstrap + 多重检验披露 ④ 分年度稳定性 | 答好加分 |
| 幸存者偏差？ | 清单是**当前在市** ETF，已清盘的不在样本内 | 主动提最好 |

### 4.2 三个岗位都要准备的「工程判断」题（本部分素材）

| 问题 | 用哪个案例答 |
|---|---|
| 你怎么处理"上游数据不可用"？ | 剔除清单读不到时选 **fail-open 放行**而不是全部剔除 —— 理由是"放行只是难看，全删会让榜单空白、让人往完全错的方向排查" |
| 不一致的数据你怎么发现？ | `verify_codes()` 每次组装校验清单与数据源的配对，不一致写进界面而不是只记日志 |
| 用户数据 vs 系统规则冲突？ | 已选列表里的旧记录**只跳过不删除**（不可逆），并披露跳过数量 |
| 你怎么保证修复是可重入的？ | `UPDATE … WHERE shares IS NULL` —— 幂等、只补不造，绝不插入脏行 |
| 你怎么验收前端改动？ | SHA256 比对线上字节流与本地产物 |

## 5. AI/Agent 编排层架构地图 ⭐ 本部分的核心增量

> **为什么单列**：`INTERVIEW_PLAYBOOK.md` 覆盖的是量化交易主链路，对整个编排层只有一句
> "多 Agent 编排用 LangGraph StateGraph，工具层自研"（该文档 `:1094`）。
> 而你要面的 **AI 应用开发 / AI 应用架构师**，问的恰恰是这一层。以下是**逐行核实过**的地图。

### 5.1 编排层：真实的图拓扑（不是"线性流水线"）

`src/orchestration/supervisor.py:819-895` 的 `build_research_graph()`：

```
START → supervisor → collect → clean → validate → store
      → verify_info → extract_events → sentiment      （信息层串行链）
      → liquidity_ctx                                  （本地技能研判）
      → analyze_{A08..A16}  ← 并行扇出（对 ALL_INSIGHT_AGENTS 逐个 add_edge）
      → recommend → audit → END
```

**要点 1：这是真扇出**（`supervisor.py:889-891` 在循环里对每个分析 Agent 同时连入、连出），不是顺序链。
**要点 2：扇出能安全合并，靠的是 state 归约器** —— `src/core/state.py:28-57` 对 `raw_points`/`agent_outputs`/`data_refs`/`trace_ids`/`errors`/`agent_messages`/`progress` 都用了 `Annotated[list, operator.add]`。
> **面试怎么讲**："并行的多个分支各自往同一个 state 字段写，**没有归约器就会互相覆盖**（后写的赢），表现为'分析结论随机少几个'。我用 `operator.add` 让它们是累加语义。"

**要点 3：分支裁剪在节点内部，不在图上**（`supervisor.py:454-497` 的 `_node()` 工厂）：
- Agent 未注册 → 返回 `{}` 直接跳过（可选分支）
- 不在本次 `plan` 里 → 跳过并写进度
- 信息层要求 `info_items` 非空、数据管线要求 `raw_points` 非空
- **单节点失败不拖垮整图**：`AgentExecutionError` 与任何异常都收敛成 `state["errors"]` 的一条记录
> **面试怎么讲**："我没有用条件边做路由，而是**在每个节点入口做门控**。好处是图结构固定、可预测、好调试；代价是扇出会创建全部节点（即使大部分会立刻返回）。对 9 个分析 Agent 这个量级，可预测性比省几个空调用更值钱。"

### 5.2 规划：LLM 动态规划 + 确定性兜底（本层最值钱的设计）

`supervisor_node()`（`supervisor.py:499-536`）：
1. 有 planner 就调 `LLMSupervisorPlanner.plan()`（`src/orchestration/planner.py:123`，`max_agents=12`，把 agent 能力表与指标表拼进 prompt）
2. **LLM 规划抛异常 → 静默回退规则式** `plan_run()`（`supervisor.py:189`）
3. ⚠️ **关键细节**：LLM 可能**漏掉大盘流动性指标**，所以用 `append_liquidity_indicators()`（`supervisor.py:123`）按任务类型/问题关键词**确定性补齐**

> **面试怎么讲（这是"给 LLM 划边界"的实证）**："规划交给 LLM，但**不让它决定完整性**。LLM 漏指标是概率事件，而'大盘流动性'这类上下文缺失会让下游分析整体失真——所以我用一条确定性规则把必选指标补回来。**LLM 负责'这次要分析什么'，代码负责'什么不能少'。**"

### 5.3 Agent 契约（`src/core/models.py:13-36`）

```python
AgentInput:  task_id, tenant_id, user_context{}, payload{}
AgentOutput: task_id, agent_id, conclusion, confidence, data_refs[],
             trace_id, reasoning_steps[TraceStep], result{}
```

- 统一 `BaseAgent` 接口 + 全异步（项目规范："所有Agent必须实现统一的BaseAgent接口"）
- `tenant_id` **贯穿每个任务**；`trace_id` 自动生成；`data_refs` 累积进 state → 最终报告可引用
- ⚠️ **注意**：`source_url/publish_time/fetch_time/raw_content_hash` 这些溯源字段**不在 AgentOutput 上**，而在 `DataPoint` 上（数据点级别）。被问"溯源怎么做的"要说到这一层。

### 5.4 ReAct 循环 + 工具注册表（A17 决策层的自主追问）

`src/orchestration/supervisor.py:666-729`：A17 在给出最终建议前，会跑一个 **ReAct 循环**，注册了 4 个工具：
`ask_agent`（追问上游分析 Agent）、`query_data`（查已采集数据点）、`load_skill` / `load_reference`（按需加载技能正文与参考）。

**⚠️ 真相（必须准确）**：`src/domain/agents/analysis/react.py:1-11` 的 docstring 明确写着：

> **"由于LLM提供商不支持原生tool calling，本模块用JSON输出模拟工具调用"**

也就是说：**没有原生 function calling，更没有 MCP**。实现方式是让 LLM 输出 `{"action": {"name", "args"}}`，代码解析后执行工具、把结果喂回，直到 `{"final_answer": {...}}` 或达到步数上限。

**代码级护栏（这部分是 AI 应用岗的高分点，`supervisor.py:670-702`）**：

| 护栏 | 实现 | 防什么 |
|---|---|---|
| **可追问对象白名单** | `askable_prefixes = ("A08".."A16")` | 数据层契约不匹配，问也问不出东西；返回提示改走 `query_data` |
| **每 Agent 追问次数封顶** | `MAX_ASKS_PER_AGENT = 1` | LLM 绕着一个 Agent 反复问、烧 token |
| **同问题幂等** | `asked_cache[(receiver, normalize_question(q))]` | 换个说法问同一件事；命中时直接回"该问题已询问过" |
| **观测截断** | `_OBS_TRUNCATE = 800` 字符 | 多步累积把 prompt 撑爆（docstring 给了算式：800×5=4000 字符上限） |
| **步数上限** | `ReActExecutor(max_steps=3)` | 死循环 |
| **prompt 级约束** | 工具 description 里写明"不要重复提问" | 与代码级护栏**双重**约束 |

> **面试怎么讲**："让 LLM 自主决定追不追问，但**自主权只在这一层**：能问谁（白名单）、问几次（封顶）、同一问题只算一次（幂等）、每次观测能带多少字（截断），全部由代码定。**Agent 的自由度是我给的额度，不是它自己发现的。**"

### 5.5 技能库：PTD 三级加载 + Token 预算（上下文工程）

`src/domain/skills/library.py:1-42`：技能是 `skills/{agent_dir}/{skill_name}/SKILL.md`（YAML frontmatter + 正文 + `references/`）。

| 级 | 动作 | 注入什么 | Token 预算 |
|---|---|---|---|
| **L0** | `list_skills` | 仅 frontmatter 摘要（name/description/tags），供 system prompt 列出"可用技能" | 单条 **<100** |
| **L1** | `load_skill` | 被选中后才返回 SKILL.md 全文 | frontmatter <150 / 正文 **<5000** |
| **L2** | `load_reference` | 正文里声明的 references 按需加载 | — |

**三道边界**（`library.py` docstring）：① 每个 agent 只能访问**映射目录**下的技能（`DEFAULT_AGENT_DIRS`）；② references 走 **frontmatter 白名单 + 路径穿越防护**；③ 质量门 `validate_all()` 校验 frontmatter 完整性与 token 预算。

> **面试怎么讲**："技能全塞进 prompt 会把上下文烧光。所以我做了**渐进式披露**：system prompt 里只放索引（每条 <100 token），LLM 觉得需要才点名加载正文，正文里引用的参考再按需加载。**加上目录级权限和路径穿越防护 —— 技能库同时是上下文预算机制和权限边界。**"

### 5.6 模型分级：把"用哪个模型"写成配置（成本/延迟的硬证据）

`configs/agents.yaml` 给**每个 Agent** 声明了 `model` 档位：

| 档位 | Agent | 说明 |
|---|---|---|
| `none`（不调 LLM） | A01 数据采集 | 纯取数，**不需要模型** |
| `local_light` | A02 清洗 / A03 校验 / A04 存储 / **A18 审计** | 本地 1.5b，短分类任务 |
| `local_medium` | A05 核验 / A06 提取 / A07 舆情 | 本地 8b |
| `deepseek-flash` | A08–A16（分析层 9 个） | 需要推理的深度分析 |
| `deepseek-v4-pro` | A17 投研建议 | 最终决策，最高档 |

> **面试怎么讲**："**模型选择是数据结构，不是 if-else。** 把'哪个 Agent 用哪档模型'写在 `configs/agents.yaml`，好处是成本/延迟的调整不需要改代码、也不会散落在各 agent 里。数据层前四个 Agent 根本不该调 LLM（`A01` 直接 `none`），这一点写进配置本身就是约束。"
> **可延伸**：这与第一部分的实测互相印证 —— 同一个消息面任务，本地 1.5b 用 1.1s，而推理模型 72~85s 还会把 max_tokens 耗在思维链上导致正文为空（见第一部分 §四）。

**单一事实源与兜底**：`src/core/agent_meta.py:1-10` 说明 agent 展示名来自 `configs/agents.yaml`，代码里留了一份**内置兜底表**（yaml 缺失/解析失败仍能展示）；并明确警告"**严禁把中文名当作路由 key**"（`agent_id` 才是稳定标识）。

### 5.7 降级链：三级短路 + 溯源标记

`supervisor.py:335-351` 的 `_storage_fallback()`：实时采集失败或为空时，从统一数据层读**最近入库快照**，并把 `extra.storage_fallback = "live_empty_or_failed"` **写进数据点**。

- 降级读取有**按指标的长度上限**（`_STORAGE_FALLBACK_LIMITS`：换手率历史 60 条、两融 10 条、北向 30 条…），保证趋势类指标仍有足够序列
- 注释写明"**禁止连接器直连数据库**"（对齐 AGENTS.md 的"所有数据访问必须通过统一数据层"）
- **降级不是静默替换**：标记会跟着数据点走到最终报告，所以"这条结论是用旧数据得出的"是可追溯的

> **面试怎么讲**："降级和造假是两件事。我允许降级（否则一次取数失败整条链路就断），但**降级必须在数据上留痕**，否则'这个结论基于什么时候的数据'就查不出来了。"

### 5.8 自修复数据源（可讲的亮点，需自己再读一遍）

`supervisor.py:357-436` 的 `_get_gap_resolver()` / `_try_self_heal()`：数据缺口时尝试让 `DataGapResolverAgent`（`src/domain/agents/data_gap_resolver.py`）**动态生成一个连接器**并重试 fetch。
配套红线在项目规范里："真实源失败后不得用模拟数据冒充"。
> 这块另有专门文档 `docs/INTERVIEW_SELF_HEALING.md`（4KB），**建议面试前读一遍**，我这次没有逐行核实。

### 5.9 LLM 网关层：已有文档覆盖，这里只补代码级要点

`INTERVIEW_会话总结_量化与AI双岗.md:122-129` 已经把这一层讲成体系（多 provider 链 / tier 路由 / 缓存 / 熔断 / 审计 / 成本），**不重复**。下面是本次逐行核实后、面试**最容易答错**的四条：

| 要点 | 事实 | 代码位置 |
|---|---|---|
| **主备必须换 provider** | `decision` 层的 fallback 从 `deepseek-flash` 改成 `local_medium`。原因：两者同属 `provider=deepseek`（同端点、同 key、**同一个熔断器**）→ DeepSeek 一熔断主备一起被拒，"等于没兜底，而本机 Ollama 明明是好的却没人用" | `configs/models.yaml:15-20`（原文注释） |
| **空响应既不算命中也不落盘** | 曾因 `max_tokens` 被思维链吃光返回空 content，那条空记录被写进缓存后**每次重试都命中它并立刻返回** —— 表现为"确定性失败、重试无用"，且耗时因命中缓存反而变快 | `gateway.py:165-170`、`gateway.py:274-276` |
| **配置类错误不计入熔断** | 缺 key / 401 / 402 / 403 标记 `count_as_failure=False`。否则"忘配 key"3 次就把熔断器打开，之后连 API 都不试、直接 `circuit_open`，把真病因淹没 | `src/core/exceptions.py:35-46`、`providers.py:146-154` |
| **缓存键含 scope，但不含租户** | 键 = `sha256(scope \| normalize(system) \| normalize(prompt))`，`scope={task_tier}\|json={0/1}` —— 防止 light 层 1.5B 的答案被 decision 层复用。**但键里没有 `tenant_id`**，缓存跨租户共享（代码事实，无注释承认） | `cache.py:29-46`、`gateway.py:161` |

> 另有两条"存在但没做"的事实，**主动说比被问出来好**：网关层**零重试零退避**（重试分散在调用方，且退避是线性的）、**无 rate limiting / 无租户额度强制**（`llm_tokens_per_month` 只用于展示）。

### 5.10 Roster 真相：声明 18 个，运行时注册 19 个，另有 1 个影子

| 声明位置 | 内容 | 一致性 |
|---|---|---|
| `configs/agents.yaml:1-135` | 18 个 Agent（id/name/layer/priority/**model**/capabilities） | 自称"单一事实源" |
| `src/core/agent_meta.py:22-41` | 内置兜底表（yaml 缺失时用） | **人工同步**，注释写着"保持同步" |
| `src/api/runtime.py:248-268` | 真实装配字典 | **19 项**（多 `A19_code_engineer`） |
| `src/orchestration/supervisor.py:30-46` | 层分组常量 | 6 层（含 `engineering`），而 `AGENTS.md:30` 写 5 层 |

- **`A19_code_engineer`**（`runtime.py:267`）不在 `agents.yaml`、不在 `agent_meta` → 前端拿到的中文名会退化成裸 ID（`agent_meta.py:74`）；一致性测试**硬编码 18 个 ID**（`tests/unit/test_agent_meta.py:12-18`）所以漏检；**它也不在 LangGraph 图里**（`supervisor.py:819-895` 建 20 个节点，无 A19），只能走 REST 手动调。
- **`DataGapResolverAgent`**（`data_gap_resolver.py:151`）：不继承 BaseAgent、不在 registry、不在 yaml —— 由 `supervisor.py:357-370` 从 A17 **借 gateway 现场 new** 出来，以 `agent_id="data_gap_resolver"` 写审计。这是个**影子 Agent**。

> **面试怎么说**："roster 有 4 处声明、靠人工同步，实测已经漂了（19 vs 18 + 1 个影子）。**正确做法是让注册表成为唯一来源、启动时断言而不是靠注释维持一致** —— 我把它记成了待办。"（这条正是本项目反复出现「同名不同值/注释与实现不一致」母题的又一例。）

### 5.11 BaseAgent 契约：15/18 真的继承，3 个靠鸭子类型

`src/core/base_agent.py:11-31`（全文 31 行）：

```python
class BaseAgent(ABC):
    def __init__(self, agent_id: str) -> None            # :18
    @abstractmethod
    async def execute(self, input: AgentInput) -> AgentOutput   # :22  ← async
    @abstractmethod
    def get_capabilities(self) -> dict[str, Any]          # :26  ← sync
    @abstractmethod
    def health_check(self) -> bool                        # :30  ← sync
```

- **A05/A06/A07 不继承 `BaseAgent`**（`info/verifier/agent.py:23`、`info/extractor/agent.py:25`、`info/sentiment/agent.py:19` 都是裸 `class Xxx:`），只是**恰好**实现了同名三方法 → 靠鸭子类型在 `supervisor.py:441` 跑通。全 `src/` **没有 `isinstance(BaseAgent)` 校验**。
- 项目规范写着"所有 Agent 必须实现统一的 BaseAgent 接口" —— **这在编译期并不强制**。这是很好的"约定 vs 强制"讨论点。

### 5.12 消息契约的三个空缺（面试常被追问的点）

`src/core/models.py:13-38`：

```
AgentInput : task_id, tenant_id, user_context, payload
AgentOutput: task_id, agent_id, conclusion, confidence, data_refs,
             trace_id, reasoning_steps, result
```

三处**规范要求但契约里没有**的东西（必须主动说清，否则被追问会崩）：

1. **无溯源字段**：`source_url/publish_time/fetch_time/raw_content_hash` 不在 `AgentOutput` 上 —— 它们在数据层内部的 `DataPoint`（`src/core/schemas.py:99-134`），跨 Agent 只传 `data_refs: list[str]`（数据 ID）。
2. **无 token / prompt_hash 字段**：这两个落在 `LLMResponse` → 审计 JSONL（`llm/audit.py:93,95-96`）→ 由 Agent 手工塞进 `result`（`analysis/base.py:337-338`）。
3. **无 disclaimer 字段**：免责声明只在三处出现 —— prompt 尾部（`analysis/base.py:278`）、A17 的 `result["disclaimer"]`、最终报告拼装（`supervisor.py:936-939`）。**"所有输出附带免责声明"没有在消息层强制。**

⚠️ **而且"标准 JSON 消息"在编排链路上没被使用**：`src/core/message.py:36-58` 定义的 `Message`（带 `audit_id`/`data_sources` + `{domain}.{action}` 格式强校验）**只被测试引用**（`tests/unit/test_core_message.py`）。真实 A2A 是 `message_bus.make_message()`（`message_bus.py:42-50`）返回的**裸 dict**，无 pydantic 校验。

### 5.13 数据溯源：规则要求"必填"，代码里没有一条 validator

`DataPoint`（`src/core/schemas.py:99-134`）逐字段核实：

| 规范要求必填 | 代码实际 | 是否强制 |
|---|---|---|
| `source_url` | `= ""`（`:118`） | ❌ **空串合法** |
| `publish_time` | `= None`（`:120`） | ❌ None 合法 |
| `fetch_time` | `default_factory=now`（`:121`） | ✅ 自动填 |
| `raw_content_hash` | 空串 + `model_post_init` 补算（`:123,129-134`） | ✅ 自动填 |
| `verified` | `= False`（`:127`） | ❌ |

- `core/schemas.py` 里**一个 validator 都没有**（全仓 23 处 `field_validator` 都在 `intraday/`、`core/config.py`、`core/message.py`）。
- 契约测试**只验默认值**，根本没测 `source_url`/`publish_time`（`tests/unit/test_data_point.py:21-27`）。
- `processed_by` 是**单值 str**，被 A02/A03 依次覆盖 → **只留最后一个处理者**，不是完整血缘链。
- **真正在"防编造"的是幻觉防护**（纯正则三项、零额外 LLM 调用），但两个调用点都传 `check_citations=False`（`analysis/base.py:303`、`recommend/agent.py:164`）→ **第三层"来源标注"在生产链路里是关掉的**。

> **面试怎么说**："规则说四个字段必填，但**代码里没有任何一条 pydantic validator 在强制它** —— 空串是合法值。所以'数据溯源'目前是**约定 + 事后审计**，不是**入口校验**。要真正强制，应该在 `DataPoint` 上加 validator，并让 A03 校验失败即拒绝入库。"

### 5.14 多租户：隔离在 API 与仓储层，Agent 层完全不设防

- `grep tenant_id` over `src/domain/agents/` → **0 命中**。19 个 Agent **没有一个**读写 `input.tenant_id`，它只是消息信封上的透传字段。
- 唯一强制点是 HTTP 中间件（`src/api/tenancy_middleware.py`），但 **`research.py` 从不读认证主体**：tenant 来自**请求体** `body.tenant_id`，默认 `"tenant_001"`（`research.py:109`）→ **认证主体与 Agent 层 tenant_id 是解耦的**。
- 鉴权**默认不强制**：`MOSS_TENANCY_ENFORCE` 未打开时走"本地开发兜底身份"，并在响应头与启动日志**显式标注未强制**（`tenancy_middleware.py:65-87`）。
- **RLS 是三层里最诚实的一层，且自己声明不是安全边界**（`src/infrastructure/security/rls.py:5-8` 原文）：
  > "**应用层 RLS 不是安全边界。** 它能防'忘了写 `WHERE tenant_id=?`'这类疏漏，防不住：拿到连接的任意代码、绕过本模块的裸 SQL、直接读库文件的运维。真正的边界是**数据库原生 RLS 策略**……加网络层 mTLS。两层都要有，缺一不可。"
- 默认部署是 **SQLite（无原生 RLS）** → 第 3 层不存在，真实隔离靠仓储层每条查询带 `WHERE tenant_id = ?`。

> ⭐ **这一段最值钱的是一句自认**（`rls.py:10-13` 原文）：
> > "⚠️ 本模块的前一版是个**假实现**：`_inject_tenant_filter` 取了租户、遍历了表，然后 `return stmt` 原样返回 —— docstring 声称的 `WHERE tenant_id = ?` 根本不存在。**那比没有更危险，因为它会让人以为已经隔离了。**"
>
> **这是全场最好的一条面试素材**：发现自己的安全代码是"假实现"、并且把"为什么假实现比没有更危险"写进注释。它同时证明了安全意识和诚实性。

### 5.15 诚实计数：18 个 Agent 要打折到 11–12 个

按"是否有独立业务逻辑"（不看 yaml 里的 capabilities 声明）：

| 档 | 数量 | 说明 |
|---|---:|---|
| **A · 有独立逻辑** | 11 | A01（Protocol 边界 + confidence 按 `publish_time` 定）、A03（三类校验）、A05（规则分 + LLM 定生死）、A06（item_id 溯源过滤 + 枚举白名单）、A07、A10（**本地估值**：行业均值对比 + 自身 PE/PB 历史分位）、A12（合规规则引擎）、A17（ReAct + A2A 幂等缓存）、A18（哈希链，零 LLM）、A19（508 行：8 源知识库 + AST 校验 + 沙箱 + 热加载）、影子 DataGapResolver |
| **B · 薄但真实** | 2 | A02（确定性规则 59 行）、A04（基本是 `save_points()` 包装） |
| **C · prompt/配置壳** | 6 | A08（55 行，零本地计算）、A09（53 行）、**A13–A16（27/28/28/28 行）** —— 只有 5 个类属性 + `system_prompt`，真逻辑在共享基类 `industry/base.py`（325 行）与 `analysis/base.py`（349 行） |

> **面试怎么说**："架构上是 18 个 Agent 分 5 层，但**按独立逻辑算，实质 Agent 约 11–12 个**；A13–A16 更像'同一个基类的 4 份配置'，行业差异体现在类属性而不是算法。这个区分很重要 —— 如果说'18 个 Agent 都是我写的能力'，被问'A13 和 A16 的算法差别在哪'就答不上来了。"

## 6. ⚠️ 诚实边界：本部分没验证 / 不能说的

**这一节比前面所有内容都重要。**

| 不要说 | 真相 | 正确说法 |
|---|---|---|
| **"我用了 MCP"** | **`src/` 里 grep `MCP` 零命中**。MCP 只出现在文档里（`AGENTS.md:31`、`ARCHITECTURE.md:51`、`PRD.md:65/200/346`）。真实机制是 `message_bus.ask_agent` + `ToolRegistry`（进程内注册表） | "工具调用是**自研的进程内注册表**，没有走 MCP server。原因是当前 LLM 提供商不支持原生 tool calling，我用 JSON 输出**模拟**了工具调用（`react.py:1-11` 有说明）。" |
| "有原生 function calling" | 同上，docstring 明说不支持 | 说"模拟"，并讲清你怎么处理 JSON 解析失败与步数上限 |
| **"18 个 Agent 都实现了统一 BaseAgent 接口"** | **A05/A06/A07 不继承 `BaseAgent`**，靠鸭子类型跑通；全仓无 `isinstance` 校验 | "规范要求统一接口，但我实测发现 **15/18 真的继承了**，信息层三个是鸭子类型。这是'约定没能变成强制'的典型 —— 补一个启动期断言就能解决。" |
| **"Agent 间用标准 JSON 消息通信"** | `core/message.py` 的 `Message`（带 `audit_id` + 格式强校验）**只被测试引用**；真实 A2A 是 `message_bus` 返回的**裸 dict** | "编排链路上跑的是轻量 dict，`core/message.py` 那套强校验消息**没有被接进主链路** —— 这是设计与实现脱节，不是设计。" |
| **"所有输出附带免责声明"** | `AgentOutput` **没有 disclaimer 字段**；免责声明只在 prompt 尾部、A17 的 result、报告拼装三处 | "免责声明在**报告层**拼装，不在**消息契约**里强制。所以中间某个 Agent 单独被调用时是没有免责声明的。" |
| **"数据溯源四字段必填"** | `source_url` 空串合法、`publish_time` 可为 None，**`core/schemas.py` 里一条 validator 都没有** | "规则写'必填'，但代码里**没有任何 validator 在强制**。所以溯源目前是'约定 + 事后审计'，不是'入口校验'。" |
| **"多租户靠 RLS 自动隔离"** | `grep tenant_id` 在 `src/domain/agents/` **零命中**；agent 层不设防；tenant 来自**请求体**而非认证主体；默认 SQLite **无原生 RLS** | "隔离发生在 **API 中间件 + 仓储层**，Agent 层只是透传字段；而且应用层 RLS **自己声明不是安全边界**（`rls.py:5-8`），真正的边界要靠数据库原生策略 + mTLS。" |
| **"18 个 Agent 都是我实现的能力"** | 按独立逻辑算**实质 Agent 约 11–12 个**；A13–A16 只有 27–28 行（5 个类属性 + prompt），逻辑在共享基类 | 见 §5.15 的口径。**别把"配置了 18 个"说成"实现了 18 个"** |
| "我知道 LLM 网关/缓存/熔断/幻觉护栏的细节" | 本次已逐行核实（见 §5.9），但 `INTERVIEW_会话总结_量化与AI双岗.md:122-129` 是更早写的，**两处可能有漂移** | 面试前把 §5.9 的四条与那份文档对一遍 |
| **"板块剔除清单是完备的"** | 123 条是**冻结清单**，东财每季度会新增报表预告板（如 `2026年报预增`） | "这是冻结清单 + `verify_codes` 自校验；**新板块需要按批补**，我把这个运维前提写进了配置文件头部" |

**我自己发现并记录的文档/代码漂移**（可以讲"我发现并记录了不一致"，但别当成已完成 —— 都还没修）：

1. `supervisor.py:719` 的工具 description 写"每个Agent最多追问**2**次"，而代码 `MAX_ASKS_PER_AGENT = 1`（`supervisor.py:677`）—— **工具描述在对模型撒谎**，模型会按"2 次"的预期提问却被拒。
2. `react.py:11` docstring 写"`max_steps=5` 时最大观测总量 4000 字符"，而 `ReActExecutor` 默认 `max_steps=3`。
3. **roster 漂移**：`configs/agents.yaml` 18 个 vs `runtime.py` 注册 19 个（多 A19）+ 1 个影子 `DataGapResolverAgent`；`AGENTS.md:30` 说 5 层、代码是 6 层。
4. **技能死映射**：`skills/library.py:39` 的键写作 `"A01_collector"`，而运行时 ID 是 `"A01_data_collector"` → 该行技能目录**永久不可达**；`:40` 的 `A05_verifier` 映射存在，但 `VerifierAgent` 没有 `_skill_library` 属性 → 同样不可达。
5. **配置与实现冲突**：`agents.yaml` 给 A02/A03/A04 标 `model: local_light`、A18 标 `local_light`，而这些类**根本不调 LLM**（`data/cleaner/agent.py:3` 自己写着"无LLM"）。
6. **`render()` 里写死的示例数字**（本次已修）：回测报告正文曾硬编码"66 条信号 / **3** 个独立时段"，实际算出 **5** —— 同一份报告正文与表格自相矛盾。现已改成从 `report.by_kind` **实时取值**（`_independence_example()`），并加注释说明为什么不能把数字写在字符串里。

> 这几条的讲法："**同名不同值、注释与实现不一致，比魔鬼数字更危险** —— 因为它不会报错，只会让模型/同事按错误的预期行动。第 6 条是我自己踩的：我把一个中间版本算错的数字写进了报告正文，表格算对了、正文没改，于是报告自己打自己。**修法不是改对那个数字，而是让它再也无法写死。**"
> （这个母题与 `docs/INTERVIEW_会话复盘_竞价重构与工程治理.md` 的坑 2「同名不同值」、以及 `llm/audit.py:85-88` 自认的"注释与实现不一致比没注释更糟"是同一个，**串起来讲很有说服力**。）

## 7. 临场前 30 分钟速查

- [ ] 能说出 **66 / 5 / 1** 这三个数：66 条信号、**5** 个独立时段（全区间）、**1** 个（样本外）
- [ ] 能解释 **为什么用「独立时段」而不是信号条数**（存量判据 → 同一事件连报多日；同系列同日触发；前瞻窗口重叠）
- [ ] 能说清 **T 与 T+1 口径的差别**，以及为什么 T+1 才可执行（份额次日 8:30 发布）
- [ ] 能说清 **为什么达标要分方向**，以及"改了口径结论仍不好、我没有放宽门槛"
- [ ] 能画出**编排图**：supervisor → 数据链 → 信息链 → 扇出分析层 → recommend → audit，并说明**归约器**为什么必需
- [ ] 能说清 **LLM 规划 + 确定性兜底**（`append_liquidity_indicators`）的分工
- [ ] 能说清 **ReAct 的四道护栏**（白名单 / 次数封顶 / 幂等缓存 / 观测截断）
- [ ] 能说清 **PTD 三级加载与 token 预算**，以及技能库同时是**权限边界**
- [ ] 能背 **模型五档**（none / local_light / local_medium / deepseek-flash / deepseek-v4-pro）并说明为什么写在配置里
- [ ] 能说清 **降级必须留痕**（`extra.storage_fallback`）
- [ ] ⚠️ 能**主动**说清"**没有 MCP、没有原生 tool calling**"，而不是等着被问出来
- [ ] 准备好"**我没验证过的三件事**"：LLM 网关细节、各 Agent 内部逻辑、板块清单的完备性
