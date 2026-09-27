# 板块拥挤度 · 周频指标口径与耗时 —— 事实核对

> 起因：2026-09-27 我（AI）在分析里写了一句
> 「每个板块 JOIN 行情表 + 资金流表做 5d/1m/2m 滚动，实测单板块 0.3-0.5s，
> **554 个板块全表 3-5 分钟**」。用户指出该口径有误。本文是核对结果。
> 结论：**那句话错在两处**，但「只算 20 日胜率过门槛的 140 个概念」是
> **主线挖掘**的口径，不是**拥挤度**的口径 —— 两者不是同一个集合。

## 一、谁是谁：四个数字，四个不同的集合

| 口径 | 数量 | 出处（可现场复验） |
|---|---|---|
| 主线监控池 `ml_board`（20 日胜率门槛后的**保留集**） | **140 概念** | `docs/MAINLINE_POOL_CURRENT.md:6` |
| 主线池级已剔除 | **184 概念** | `docs/MAINLINE_POOL_CURRENT.md:7` |
| 拥挤度看板清单**可见**数 | **554** | `sector_crowding_list WHERE visible=1` |
| 拥挤度周频 4 列**实际计算**数 | **1878 ~ 1879 板块** | `sector_crowding_metric` 周 `2026-W39` = 1879 行 |
| 周频 4 列**实测耗时** | **9.3 秒** | `docs/MAINLINE_CROWDING_PURIFIED.md:17` |

## 二、我的两个错误

### 错误 1：耗时张冠李戴（把「一键刷新」的耗时安到了「周频指标」上）

* `3~5 分钟` 是**一键刷新 / 日更**的量级 —— 冷缓存时要**按板块联网抓成分股**。
* 周频 4 列指标是**纯本地 SQL**：`_members_for_sector()` 先取提纯名单，
  再命中本地 `sector_crowding_member` 缓存就直接读（`metrics.py:269-277`），
  只有缓存过期才回落到 `sources.fetch_board_members()`。
* 实测：**1878 个板块，9.3 秒**（`docs/MAINLINE_CROWDING_PURIFIED.md:17`）。

### 错误 2：口径张冠李戴（把「看板可见数」当成了「参与计算数」）

* `554` 是**看板清单可见数**，决定前端表格渲染多少行，**不决定后端算多少**。
* 后端算的是 `compute_water_changes()` 返回的全部板块（`metrics.py:459`），
  实测 **1878/1879**。

### 附带发现：三处 UI / 文档文案是旧数

都在说「**900+ 板块**」，与库里实际的 1878 不符（且 3~5 分钟只在冷缓存成立）：

* `web/src/components/useCrowdingMetrics.ts:18`
* `web/src/components/SectorCrowdingTab.tsx:101`
* `src/api/routes/sector_crowding.py:182`
* `docs/SECTOR_CROWDING.md:164`

## 三、「20 日胜率 > 45% 的 ~130 个概念」是哪里的口径

是**主线挖掘**的，不是拥挤度的。

* 门槛实现：`src/mainline/theme_gate.py`、`src/mainline/alert_returns.py:87`
  `DEFAULT_MIN_WIN_RATE = 0.4` —— **代码默认是 40%，不是 45%**。
  `0.45` 只出现在 `scripts/build_theme_exclusions.py` 的**示例命令**里。
* 现行生效口径写在 `configs/mainline_theme_exclusions.yaml:5` 的文件头：

  > 剔除 = **20 日胜率 <= 40%** 且 已走满的 20 日窗口 >= **3** 个

  门槛是**严格大于**；判不准的（窗口 < 3 的新概念）**留在池内继续攒证据**。
* 台账规模：`mainline_theme_exclusions.yaml` 34 个、
  `concept_removal_batches.yaml` 184 个池级剔除 → 最终在监控 **140 概念**。
  用户口述的「大概 130 个左右」对得上这 140。

## 四、为什么拥挤度**没有**跟着只算 140（这是有意为之，不是漏改）

`src/sector_crowding/metrics.py:58-96` 的 docstring 写明了取舍：

* 拥挤度的黑名单 = `load_removed_concepts()`（184）∪
  `load_crowding_exclusions()`（82），合计 **266**。
* **刻意不吃整份 `sector_blacklist.yaml`（606 条）**，因为那里面大头是历史遗留
  （淘汰的 `865xxx` 概念体系、GICS 行业、地域板块），而拥挤度模块**刻意保留**它们
  —— 原话：「以后想看行业拥挤度不用重跑 6 年」。
* 代价量化过：按整份过滤会把最新一周 **1878 砍到 1541**，多砍的 337 个绝大多数是
  行业指数，属**过度过滤**。

**所以：「其他概念板块都剔除了」对主线监控池成立，对拥挤度周频指标不成立。**

## 五、已实施：收到看板**默认视图**（方案 B，两轮修正）

2026-09-27 用户裁定走 **B**。**第一版收错了范围**，见下。

| 改动 | 位置 |
|---|---|
| 新增 `MetricConfig.pool_only: bool = True` | `src/sector_crowding/config.py` |
| 新增 `MetricConfig.pool_concepts_only: bool = True` | 同上 |
| 新增 `_scope_to_pool()`（可单测的收口函数） | `src/sector_crowding/metrics.py` |
| `compute_all_metrics()` 改调 `_scope_to_pool()` | 同上 |
| `set_metric_meta()` 落 `pool_only` / `pool_concepts_only` 留痕 | 同上 |
| 配置项 + 取舍注释 | `sector_crowding/config.yaml` |
| 5 处「900+ 板块约 3~5 分钟」旧文案改对 | 见下 |
| 5 条回归测试 | `tests/unit/test_sector_crowding.py` |

### ⚠️ 第一版收错了：554 vs 262

第一版 `_scope_to_pool()` 用了 `list_visible_codes(concepts_only=False)`
→ 取到 **554**。但看板的「只看概念板块」勾选框**默认是勾上的**
（`web/src/components/SectorCrowdingTab.tsx` → `useState(true)`，
即请求 `config_list?concepts_only=true`），用户实际看到的清单只有 **262**。

**差的那 292 个是非概念板块**（行业指数 / 地区 / 指数样本 / 同花顺自建组合，
见 `is_concept_board`）。收口的**唯一理由**是"别算渲染不出来的东西"，
所以判据必须等于**默认渲染集合**，而不是"可能被渲染的集合" ——
勾选框是逃生口（它自己的 tooltip 就写着"数据本身是全量落库的，取消勾选即可查看全部"）。

### 实测收口效果（只读验证，`scripts/_verify_pool_only.py`）

```
水位变化算完：as_of=20260923  用时 2.50s

compute_water_changes 返回（= 全量）      :  1479
list_visible_codes(concepts_only=False)   :   554
list_visible_codes(concepts_only=True)    :   262

收口 B'（可见=全部） 实际待算             :   554
收口 B''（可见=概念）实际待算【现行默认】 :   262

相对全量少算（现行默认）: 1217 个（82.3%）
```

> 注：这里全量是 **1479** 而不是 1878 —— `compute_water_changes()` 会丢掉
> "最后一天 ≠ 全库最新交易日"的板块（停牌/停更）。1878 是 `sector_crowding_metric`
> 里 `2026-W39` 的**历史落库行数**，两者口径不同，别混。

### 三个防呆（都有测试钉住）

1. **清单为空 → 退回全量并告警**，不是算成"本周 0 个板块"。
   后者会落成 0 行却报 `done` —— 像一个成功的空周，且**不抛任何异常**。
2. **`pool_only=False` 原样返回**，回填历史周 / 做对照实验要有退路。
3. **`pool_concepts_only` 默认 True**，且断言它等于看板默认勾选状态 ——
   第一版就是这里写错，而且**不会让任何断言失败**。

### 文案改动（5 处）

旧文案两处都不准：`900+ 板块` 是旧数；`3~5 分钟` 只在**冷缓存抓成分股**时成立。

* `web/src/components/SectorCrowdingTab.tsx` —— 按钮 title
* `web/src/components/useCrowdingMetrics.ts` —— 模块注释
* `web/src/components/SectorCrowdingAlertPanel.tsx` —— 未算过的提示
* `src/api/routes/sector_crowding.py` —— 接口 docstring
* `docs/SECTOR_CROWDING.md` —— 接口说明

统一口径：**只算当前视图里的概念板块（约 262 个）；热缓存几秒，冷缓存要抓成分股，可能 3~5 分钟。**

## 六、黑名单固化：把「已剔除」记住（2026-09-27 第二轮）

用户口径：

> 记住被剔除的几百个概念板块，拉入黑名单，不进行任何拥挤度计算，
> 不妨碍后续有新概念板块可以加进来。

### 为什么收口还不够、还要黑名单

`pool_only` 只挡**周频指标**。日更（`refresh_all_incremental`）靠
`pool_only=True` 顺带躲开那些板块，但**全量刷新（`pool_only=False`）
仍会把它们重算一遍**，而且没有任何东西"记住"这些是已经决定不要的。

### 收的是哪一批：`visible = 0`

| 集合 | 数量 | 其中概念板块 |
|---|---:|---:|
| 清单 `visible = 0`（用户从看板移除） | **671** | **670** |
| 清单 `visible = 1` | 554 | 262 |

671 里 670 个是概念板块 —— 与用户说的「被剔除的几百个**概念**板块」对得上。

### 怎么落的：扩 `prune_crowding.py`，不手改配置

`configs/crowding_exclusions.yaml` 是**生成文件**，文件头明确写着
「不要直接编辑本文件」。所以给生成器加了**第二个来源**：

```
1. reason: 点名      —— 2026-09-22 点名那 82 个（名称 → 库里查代码）
2. reason: 看板移除  —— visible = 0 的全量快照（671）        ← 新增
```

重跑后 `count: 671`（82 点名 ∪ 671 看板移除，按 code 去重）。
这一批代码**本来就来自库**，不经过"名称 → 代码"解析，
所以没有本文件顶部记录的那种手写代码删错板块的风险。

### 为什么不会挡住新概念

剔除清单是**冻结快照**，不是"凡不在可见池里就排除"的动态规则。

新概念板块不在快照里 → 照常被 `seed_list` 种进清单 → 照常参与计算。

⚠️ 反过来做（动态规则）会**死锁**：新板块不在可见池 → 被判剔除 →
`seed_list` 跳过它 → 永远进不了可见池。

### 实测生效验证（`scripts/_verify_blacklist.py`）

```
load_crowding_exclusions()          : 671
metrics._sector_blacklist() 合并后   : 672

可见（全部）                        : 554
可见（概念 only）                   : 262

[!] 可见板块里被黑名单挡住的         : 0      ← 零误伤
日更池实际（可见 - 黑名单）          : 554    ← 不变
指标池实际（可见概念 - 黑名单）      : 262    ← 不变
```

即：**当前计算集合一个没变**，黑名单是纯增量保险 ——
以后无论谁跑全量刷新，那 671 个都不会再被算。

### ⚠️ 顺带修掉一个静默陷阱

清单一旦收入某个板块，`db.query_list_view()` 就会把它过滤掉。
而 `POST /config_list`（新增）与 `POST /config_list/restore`（恢复）
原来只写 `visible = 1`、**不碰清单** —— 结果是接口回
`{"restored": 1}` 而板块**不出现**，用户只会以为"恢复坏了"。

修法：新增 `config.remove_crowding_exclusions(codes)`，
两个入口在写入后自动把代码从清单里摘掉（用户的显式恢复 =
撤销当初的剔除决定）。它按行改写、**保留文件头**，只删命中的
`- { code: ... }` 行并同步 `count:`。

> 注：`/config_list/hidden`（「已隐藏板块」列表）**不受**黑名单影响
> —— `db.query_hidden_boards()` 不过滤黑名单。所以那 671 个仍然看得到、
> 勾得动、恢复得了。

## 七、可现场复验的命令

```sql
-- 看板可见数
SELECT COUNT(*) FROM sector_crowding_list WHERE visible = 1;              -- 554
-- 概念板块（= 周频 4 列现行计算范围）
SELECT COUNT(*) FROM sector_crowding_list l LEFT JOIN sector_meta m
  ON m.sector_code=l.sector_code
 WHERE l.visible=1 AND COALESCE(m.is_concept,1)=1;                        -- 262
-- 已剔除（= 黑名单固化范围）
SELECT COUNT(*) FROM sector_crowding_list WHERE visible = 0;              -- 671
-- 周频实际计算数（最新一周，跑完 force 后为 262）
SELECT compute_week, COUNT(*) FROM sector_crowding_metric
 GROUP BY compute_week ORDER BY compute_week DESC LIMIT 2;
-- 日更覆盖
SELECT COUNT(DISTINCT sector_code) FROM sector_crowding_daily;           -- 2510
SELECT COUNT(DISTINCT sector_code) FROM sector_crowding_daily
 WHERE trade_date = (SELECT MAX(trade_date) FROM sector_crowding_daily);  -- 1479
```

## 八、force 重算实测（2026-09-27）

```
=== BEFORE ===
  2026-W39: 1879 行
  2026-W38: 1878 行

pool_only=True  pool_concepts_only=True

=== RESULT ===
  status       : done
  week         : 2026-W39
  total        : 262 个板块
  rows_written : 262
  failed       : 0 个
  task.seconds : 3.37 s
  wall clock   : 3.39 s

=== AFTER ===
  2026-W39: 1879 行
  2026-W38: 1878 行

[!] 本周陈旧行（库里还在、但本轮未更新）: 1617
```

**收口后实测 3.37 秒**（对照：收口前同规模全量记的是 9.3 秒 / 1878 板块）。

⚠️ 注意 `AFTER` 本周仍是 1879 行：`upsert_metrics` 只更新它算的那些，
**不会删**不再计算的行的旧值。也就是 W39 里有 **1617 行是上一轮全量留下的陈旧值**。
跑 `force` 一轮之后它们仍是旧数。要清掉见 `db.clear_metrics`（慎用，那是清全表）。
