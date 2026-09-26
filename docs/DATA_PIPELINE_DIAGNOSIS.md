# 投研数据链路诊断报告：三个问题的根因与修复

> 触发案例（用户原问题）：
> 「当前宏观环境如何？对A股有什么含义？美国说年内还会再加息一次，
>   请对当前A股AI产业链各细分行业龙头的估值及10月走势走分析预测。」
>
> 诊断方法：代码走查 + 本机实测（真实 DB 2,473 MB / 749 个指标 / 真实数据源 / 真实 LLM）
> 日期：2026-09-26

---

## 结论速览

| 你的怀疑 | 是否成立 | 实际情况 |
|---|---|---|
| 1. 每次都从互联网采集，没有按更新周期定期采集 | **部分成立** | **scheduler 有 37 个作业在采**（宏观/中观/微观/盘中全覆盖）。慢的真凶是 **1 个必然失败的外网指标拖了 23.5 秒**，加上 **2 个指标完全没有 TTL 导致每次全量重拉**（13.4s） |
| 2. 数据缺失是因为没查本地库 | **不成立** | 库**查了**，路由的 DB-first 逻辑是好的。真凶是 **LLM 规划器返回了违反契约的裸指标名**，A01 拿它去 fetch 时**没有任何连接器 supports**，瞬间失败 |
| 3. Agent 漏查数据库 | **不成立** | 没有漏查。但**有 5 个指标被设计成"故意跳过数据库"**，其中 2 个代价极高 |

---

## 一、问题 2 的根因：指标契约违规（这是"估值数据缺失"的真凶）

### 1.1 实测复现

`AnalyzeRequest.analysis_type` 默认 `"full"`，前端传 `analysisType`；
LLM 规划器（light 层 `qwen2.5:1.5b`）对这条问题的真实输出（实测，命中缓存 0.0s）：

```json
{
  "analysis_type": "macro",
  "target": "",
  "agents": ["A08_macro","A10_micro","A11_fin_risk","A12_compliance","A17_recommend","A18_audit"],
  "indicators": ["us_fed_rate","stock_close","PE(TTM)","PB","资产负债率","流动比率"]
}
```

**问题在两处：**

1. `"target": ""` —— 没有标的代码
2. `"PE(TTM)"`、`"PB"`、`"stock_close"` 是**裸名字** —— 个股指标必须拼 6 位代码
   （`PE(TTM):300308`）

### 1.2 为什么裸名字必然失败

实测 A01 采集这些指标的返回：

```
PE(TTM)        -> EXC DataFetchError      0 ms
PB             -> EXC DataFetchError      0 ms
资产负债率      -> EXC DataFetchError      0 ms
流动比率        -> EXC DataFetchError      0 ms
stock_close    -> EXC DataFetchError      0 ms
```

**瞬间失败，因为没有任何连接器 `supports()` 裸名字。**
对比正确的带后缀形态：

```
PE(TTM):300308     -> 1096 点   302 ms   最新=2026-09-25
PB:300308          -> 1096 点   227 ms   最新=2026-09-25
stock_close:300308 -> 3348 点   901 ms   最新=2026-09-24
```

### 1.3 所以本地库**根本不缺这些数据**

```
fact_data_points 表：1,996,561 行，749 个不同指标
  stock_close:  覆盖 536 个标的，最新 2026-09-24
  PE(TTM):      覆盖  82 个标的，最新 2026-09-24
  PB:           覆盖  80 个标的，最新 2026-09-24
  ind:sw_third_pe_ttm:all   1645 行（申万 335 个三级行业 PE 截面），最新 2026-09-22
  资产负债率:     仅   1 个标的
  流动比率:       仅   1 个标的
```

**82 只个股的 PE、80 只的 PB、335 个行业的估值截面都在库里且是最新的。**
"估值无任何输入数据 / valuation_calc=数据不足"**不是数据缺失，是指标名没拼上代码**。

### 1.4 还有一个更深的坑：`full` 类型结构上不含个股指标

```python
_PLANNING = {
    "macro":    (["CPI","PPI"], ["A08_macro"]),
    "industry": (["CPI","PPI"], ["A09_meso"]),
    "stock":    (["stock_close","PE(TTM)","PB","资产负债率","流动比率"],
                 ["A10_micro","A11_fin_risk","A12_compliance"]),
    "full":     (["CPI","PPI"], list(ANALYSIS_AGENTS)),   # ← 只有 CPI/PPI
}
```

实测：

```
plan_run("full", "300308") -> ['CPI','PPI', + 10 个流动性指标]   ← 无 PE/PB
plan_run("stock","300308") -> ['stock_close:300308','PE(TTM):300308', ...]  ← 正确
```

**即使 target 有代码，`full` 类型也不会带个股指标。** 而 `industry` 类型实测是好的：

```
plan_run("industry", "A股AI产业链各细分行业龙头")
  -> agents: [A09_meso, A13_tech, A05, A06, A07, A17, A18]
  -> indicators: [..., 'ind:半导体销售额同比', 'ind:sw_third_pe_ttm:all',
                  'ind:sw_third_pb:all', 'ind:penetration:AI大模型应用', ...]
```

**所以：正确路由到 `industry` 就能拿到行业估值截面。**
LLM 规划器把它定成 `macro`，是这条链断裂的起点。

### 1.5 修复

新增 `sanitize_indicators()`（`src/orchestration/supervisor.py`），在 LLM 规划之后
**确定性修正契约**：

| 情况 | 处理 |
|---|---|
| 有 6 位标的代码 + 裸个股指标 | 补后缀：`PE(TTM)` → `PE(TTM):300308` |
| 无标的 + 裸个股指标 | **丢弃**，并换成行业估值截面 `ind:sw_third_pe_ttm:all` 等 |
| 已带后缀 | **原样保留**（不能用 target 覆盖 LLM 明确指定的标的） |

为什么"换成行业估值"而不是静默丢弃：丢弃后 A10 仍然没有估值素材，
还会输出"估值数据不足"；换成行业截面（335 个行业、有真实数据源与入库作业），
至少让分析层有据可依。修正动作写入 `progress`，用户能看到。

实测效果：

```
planner 原始: ['us_fed_rate','stock_close','PE(TTM)','PB','资产负债率','流动比率']
无标的 -> ['us_fed_rate','ind:sw_third_pe_ttm:all','ind:sw_third_pb:all','ind:sw_first_pe_ttm:all']
有300308 -> ['us_fed_rate','stock_close:300308','PE(TTM):300308','PB:300308',
             '资产负债率:300308','流动比率:300308']
```

---

## 二、问题 1 的根因：慢在三个具体的地方

### 2.1 scheduler **确实**在定期采集（先澄清这一点）

`src/scheduler/registry.py` 里 **37 个作业**，与投研指标直接相关的：

| 作业 | cron | 采什么 |
|---|---|---|
| `snapshot_macro` | `30 18 * * *` | CPI/PPI |
| `industry_valuation_snapshot` | `0 16 * * 1-5` | 申万一/二/三级 PE/PB 截面 |
| `tech_industry_daily` | `30 16 * * 1-5` | 科技行业 PE-TTM |
| `tech_industry_monthly` | `30 9 20 * *` | 半导体销售额同比 |
| `penetration_rate_update` | `0 9 1 * *` | 渗透率 |
| `market_intraday_snapshot` | `*/5 9-11,13-14 * * 1-5` | 两市成交额/换手率 |
| `market_daily_snapshot` | `10 16 * * 1-5` | 成交额60日序列/两融/北向/宽基估值分位 |
| `board_cyb_kcb_intraday` | `*/5 9-11,13-14 * * 1-5` | 双创实时成交额 |
| `board_cyb_kcb_daily` | `20 16 * * 1-5` | 双创估值分位/个股截面 |
| `fedwatch_daily` | `30 9 * * *` | CME FedWatch |
| `quant_data_sync` | 见注册表 | 个股日线/PE/PB |

**所以"没有按更新周期定期采集"这个判断不成立** —— 覆盖面是完整的。

### 2.2 但有 **1 个指标把整段采集拖到 23.5 秒**

实测用户问题规划出的 13 个指标逐个耗时：

```
CPI                              229 点     0.01s
PPI                              229 点     0.01s
mkt:turnover:total                 1 点     0.29s
mkt:turnover:hist                 60 点     2.73s
mkt:turnover_rate:all_a            1 点     2.91s
mkt:margin_balance                 1 点     1.18s
mkt:margin_balance:hist           10 点     0.98s
mkt:north_flow                     1 点     1.01s
idx_val:snapshot:all               5 点     5.95s
mkt:cybkcb:turnover:all            3 点     0.07s
mkt:cybkcb:val:all                 2 点     1.23s
mkt:cybkcb:spot_summary            1 点    13.41s   <== 慢
fed:rate_prob:next                 0 点    23.47s   <== 慢，而且永远返回空
```

`fed:rate_prob:next` 的真实报错（实测）：

```
CME FedWatch不可达: Failed to perform, curl: (28) Failed to connect to
www.cmegroup.com:443 after 21277 ms: Could not connect to server
```

**A01 用 `asyncio.gather` 并发采集，墙钟 = 最慢那个 = 23.5 秒。**
而且它**没有失败记忆** —— 下一个用户再问，重新等满 23.5 秒。

### 2.3 还有 **2 个指标每次全量重拉**

`mkt:cybkcb:spot_summary`（13.4s）抓的是东财 clist **分页拉创业板+科创板全部个股**
（~16 次分页请求，每页间隔 0.15s）。它**不在任何 TTL 规则里**，
`_cache_ttl()` 返回 `None` → **每次分析都重新全量拉**。

### 2.4 修复

| 修复 | 文件 | 效果（实测） |
|---|---|---|
| **数据源失败冷却**（negative cache） | `connectors/source_cooldown.py`（新增） | 连续 5 次 fetch：**117s → 23.3s**（首次 23.3s，其余 0.00s） |
| **启动预探外网源** | `api/main.py` `_warm_external_sources` | 冷却在用户到来前就位 → **第1轮 23.5s → 第2轮 0.0s** |
| **给 spot_summary 加 5 分钟 TTL** | `connectors/router.py` | 从"每次全量重拉"变成 5 分钟内命中 |

冷却机制：失败记 60s 冷却、连续失败指数退避（封顶 900s）、成功立即清除。
按 `源:指标` 隔离 —— 一个指标不通不影响同源其他指标。

---

## 三、问题 3 的核查结果：没有漏查，但有 5 个"故意跳过"

我把三处口径并排打出来了（`scripts/data_lifecycle.py`）：

```
指标                       频度  周期 滞后 过期  TTL    库策略            自动采集作业
CPI                       月频   30  90  365  24h    查库优先          snapshot_macro
PE(TTM):300308            日频    1   7   30  12h    查库优先          quant_data_sync
ind:sw_third_pe_ttm:all   日频    1   7   30  12h    查库优先          industry_valuation_snapshot
mkt:turnover:total        日频    1   7   30  300s   ★跳过库(实时型)    market_intraday_snapshot
mkt:cybkcb:spot_summary   日频    1   7   30  300s   ★跳过库(实时型)    board_cyb_kcb_daily
fed:rate_prob:next        日频    1   7   30  1h     ★跳过库(实时型)    fedwatch_daily

指标数 28｜无定期作业 0｜分析时跳过数据库 5
```

**核查结论**：
- 路由的 DB-first 逻辑是**正确实现的**（`_cached_fetch` 三级短路：
  TTL → 本地 DB → connector 链，DB 命中就跳过网络）
- 确实存在 **5 个指标被 `_DB_SKIP_PREFIXES` 标记为"实时型"**，永远绕过数据库
- **"跳过库"不等于"不写库"** —— 采集成功后仍会回填 DB。`fed:` 之所以 0 行，
  是因为它**从来没成功过**（外网不通），不是没写

### 顺带查出并修掉的两个口径 bug

1. **`mkt:cybkcb:` 完全不在新鲜度规则里** → 落到默认月频 (30/90/365)，
   被当成"30 天前的都算新鲜"。而它实际是盘中每 5 分钟采的实时数据。
   已显式加入日频段（`core/data_freshness.py`）。

2. **`mkt:cybkcb:spot_summary` 无 TTL** → 每次分析全量重拉。已加 5 分钟。

### 另外补上 6 个"有需求但没作业"的指标

`data_lifecycle.py --check` 曾列出 6 个**没有任何定期作业**的指标：

```
us_cpi_yoy / us_fed_rate / us_nonfarm / us_pce    （用户问"美国加息"时要用）
资产负债率:{code} / 流动比率:{code}
```

新增 2 个作业：

- `us_macro_daily`（每日 08:40）—— 采美国宏观 6 个指标
- `financial_ratio_weekly`（每周五 16:50）—— 标的从**库里已有 PE 的标的**反查
  （写死清单几天就过期；反查天然与估值覆盖同步）

现在：**无定期作业 0 个。**

---

## 四、必须如实说明的一件事：美国利率数据源本身是陈旧的

新增作业后我实测了这些指标能不能采到，结果发现一个**数据源层面**的问题：

```
us_cpi_yoy         223 点  最新=2026-08-01   ← 正常
us_fed_rate        291 点  最新=2025-07-31   ← 陈旧 14 个月
us_nonfarm         668 点  最新=2025-08-01
us_pce             666 点  最新=2025-08-29
us_core_cpi        668 点  最新=2026-08-12
```

直接查 AkShare 原始接口确认（`macro_bank_usa_interest_rate`）：

```
291  美联储利率决议报告  2025-07-31   4.5   4.5  4.5
292  美联储利率决议报告  2025-09-18   NaN   NaN  4.5   ← NaN 占位
293  美联储利率决议报告  2025-10-30   NaN   NaN  NaN   ← 全 NaN
```

**AkShare 的美联储利率源最新有效值停在 2025-07-31，之后是上游未回填的 NaN 占位。**

这意味着：**你问"美国说年内还会再加息一次"，系统其实拿不到当前美联储利率。**
这是一个**必须让用户看见的数据缺口**，不能靠作业掩盖 —— 作业只能把已发布的部分入库。

好消息是这条链路已有的设计是对的：
- `A08_macro` 的 system_prompt 明确要求"缺 FedWatch 时**声明数据缺口并按已有数据给方向**，禁答「无法判断」"
- `DataFreshnessEvaluator` 会按指数衰减给出低置信标注
- `us_cpi_yoy`（2026-08）是新鲜的 → 通胀趋势可用作方向判断

**但"当前政策利率"这个数在这个源上就是拿不到。**

---

## 五、改动清单

| 文件 | 改动 |
|---|---|
| `src/orchestration/supervisor.py` | 新增 `sanitize_indicators()`（指标契约修正）；`extract_topic_keywords()` 支持 full/stock 类型同时取宏观+行业关键词 |
| `src/infrastructure/connectors/source_cooldown.py` | **新增**：数据源失败冷却（negative cache） |
| `src/infrastructure/connectors/fedwatch_connector.py` | 接入失败冷却 |
| `src/infrastructure/connectors/router.py` | `mkt:cybkcb:spot_summary` 加 5 分钟 TTL；`mkt:cybkcb:turnover:all` 显式 TTL |
| `src/core/data_freshness.py` | `mkt:cybkcb:` 显式归入日频（原来落默认月频） |
| `src/scheduler/registry.py` | 新增 `us_macro_daily`、`financial_ratio_weekly` |
| `src/scheduler/jobs.py` | 新增 `generic_indicator_snapshot` kind + 标的反查 |
| `src/api/main.py` | 新增 `_warm_external_sources()` 启动预探 |
| `scripts/data_lifecycle.py` | **新增**：三处口径并排总览（发现"没作业/不查库"指标的工具） |
| `tests/unit/test_research_data_pipeline_fixes.py` | **新增**：15 个回归测试 |

### 顺带修掉的信息层饥饿（问题 1 的隐性代价）

同一问题还暴露：`extract_topic_keywords()` 用的是**精确词匹配**，
而用户写的是"**再加息**"（不含"美联储"字样）→ 命中为空；
且 `analysis_type="macro"` 时 industry 关键词组**根本没被评估**。
结果 `topic_keywords=[]` → collect 节点**一个新闻都不拉** →
A05/A06/A07 拿到零输入全部空跳过，**舆情/事件维度整体缺席**。

修复后：

```
analysis_type=macro     关键词 3 个：['加息','美联储','降息']
analysis_type=full      关键词 24 个：['加息','美联储','降息','人工智能','AI','算力','芯片',...]
```

---

## 六、还没做 / 需要你决策的

| 项 | 说明 |
|---|---|
| **行业龙头 → 个股代码映射** | 用户问"AI产业链各细分行业龙头"，系统**无法把行业名解析成具体股票**（`route_industry` 只路由到 A13-A16 行业 Agent，不产出个股列表）。这是"分析龙头个股估值"的结构性缺口。需要一张"细分行业 → 龙头标的"映射表（可用申万三级行业成分股 + 市值/成交额排序自动生成） |
| **多标的批量估值** | 即使有了映射，`_PLANNING` 的 `stock` 类型也只支持**单个** target。要分析"各细分龙头"需要支持指标列表带多个代码 |
| **把"跳过库"的实时指标改走 DB** | 4 个实时指标（成交额/换手率/双创截面）盘中每 5 分钟采，理论上可以查 DB 短 TTL。但要先确认"DB 里的值滞后一个采集周期"是否可接受 |
| **FedWatch 数据缺口的产品化** | 建议在报告里明确输出"当前美联储利率数据不可用（源停在 2025-07-31）"，而不是只靠置信度衰减暗示 |
