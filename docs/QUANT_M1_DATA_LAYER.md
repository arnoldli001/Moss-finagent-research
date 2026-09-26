# 多因子回测 M1：数据层（PIT 基本面 + 前复权价格）

> 本文只记录 **M1（数据层）** 的实测结论与硬规则，是设计文档
> 「数据获取 → 因子计算 → 因子筛选 → 因子合成 → 条件组合回测」里第一段的落地记录。
> 全部结论均为 2026-09-15 在本机实测所得（数据源、行数、公告日、覆盖率都是真数）。

---

## 一、为什么先做数据层：设计文档的数据前提不成立

设计文档 §2.1 把 Tushare Pro 定为主数据源，并注明「零预算也能使用」。核对官方文档后：

| 接口 | 积分要求 | 出处 |
|------|----------|------|
| `daily_basic`（PE/PB/换手/股息/市值） | **至少 2000 积分** | [doc 32](https://tushare.pro/document/2?doc_id=32) |
| `fina_indicator`（ROE/毛利/增速…） | **至少 2000 积分**，且**只能按单只股票取**（每次≤100 条） | [doc 79](https://tushare.pro/document/2?doc_id=79) |
| 全市场单季财务横截面 | 需 `fina_indicator_vip` = **5000 积分** | 同上 |
| `moneyflow`（资金流） | **至少 2000 积分** | [doc 170](https://tushare.pro/document/2?doc_id=170) |
| `daily`（日线行情） | 基础积分可用 | [doc 27](https://tushare.pro/document/2?doc_id=27) |

注册只送 100 分，2000 分需靠写文章/提 bug/赞助获得且**一年有效**
（[积分办法](https://tushare.pro/document/1?doc_id=13)）。本机 `.env` 也**没有**
`TUSHARE_TOKEN`（`src/core/config.py:66` 在读它）。

结论：**M1 走 AkShare + QMT，不依赖 Tushare。** 将来有 token 时再加一个
`TushareSource` 做交叉校验即可（接口位置已留好）。

---

## 二、AkShare 财务接口：只有「东财业绩报表」给公告日

逐个接口核对列名与内容（`600519` / 报告期 `20260630`）：

| 接口 | 规模 | 公告日 | 用途 |
|------|------|--------|------|
| `stock_yjbb_em(date=报告期)` | 11449 行 × 16 列 | **有**（`最新公告日期`） | ✅ **PIT 骨架** |
| `stock_financial_analysis_indicator` | 10 行 × 86 列（逐股） | 无（只有`日期`=报告期） | ✅ 字段补充 |
| `stock_financial_abstract` | 80 行 × 105 列 | 无 | 备用 |
| `stock_financial_report_sina` | 103 行 × 147 列 | 无 | 备用 |
| `stock_financial_abstract_ths` | 报告期列 | 无 | 备用（需子进程隔离） |

**没有公告日的接口不能进 PIT 面板。** 新浪系接口可以做字段补充，但时点必须取自业绩报表。

---

## 三、实测发现的两个数据陷阱（都已修）

### 陷阱 1：业绩报表里 54% 不是沪深A股

报告期 `20260630` 的 11449 行，市场构成实测：

| 市场 | 行数 | 行业字段缺失 |
|------|-----:|-------------:|
| 沪主板 600/601/603/605 | 1702 | 0 |
| 科创板 688/689 | 618 | 0 |
| 深主板 000/001/002/003 | 1495 | 1 |
| 创业板 300/301/302 | 1410 | 1 |
| **4xx/8xx/920（新三板 + 北交所）** | **6186** | **5798** |
| 其他前缀 | 93 | — |

即：**沪深A股只有 5225 只，另外 6186 行是流动性、披露规则、行情可得性都不同的新三板/北交所**。
不过滤的话因子截面会被这批股票占满，回测结论直接跑偏。

处理：`fundamental_source.market_of()` + `filter_universe()`，默认 `universe="a_share"`
只留沪深A股，并把**各市场行数写进日志**（不静默丢数据）。过滤后行业缺失从 5800 行降到 **2 行**，
反证这批确实是同一批非沪深标的。

> 北交所（43/83/87/88/920）与新三板（400/430/83x/87x…）**前缀重叠**，仅凭代码无法区分，
> 需要交易所标的名录（计划在 M2 用 QMT 的标的名录锁定）。因此 `market_of` 如实返回
> `bse_or_neeq`，不假装能分清；M1 默认两者都不纳入。

### 陷阱 2：尚未披露的报告期会让接口报内部错误

请求 `20260930` / `20261231`（当时是 2026-09-15）稳定报
`TypeError: 'NoneType' object is not subscriptable` —— akshare 拿到空响应后的内部错误。
这不是"取数失败"，而是"数据还没出生"。

处理：`report_periods(..., as_of=today, grace_days=15)` 直接剔除
`报告期结束 + 15 天 > 今天` 的period（A 股首份季报/半年报通常 T+10~T+20）。
改后 2026 全年同步 **零失败**。

---

## 四、PIT 的三条硬规则

1. **只认公告日**：没有 `ann_date` 的记录在规范化阶段就丢掉；
2. **公告后再等 `lag_days`（默认 1 个自然日）**才可使用 —— 财报多在盘后披露，
   当天知道当天用属于"隔空取物"，保守起见隔日生效（`PitConfig.lag_days`）；
3. **只向前取值**：`as_of(t)` 取「可用日 ≤ t」中最新的一条，不插值、不看未来。

一个容易被做错的地方：**修正公告要保留多版本**。业绩快报 → 正式财报、财报更正，
是同一 (code, 报告期) 的**不同公告日版本**；若只保留最新版，修正公告发布之前的截面会
看不到这只票（明明是已公告过的），等于凭空丢数据。去重键因此是
`(code, report_period, ann_date)`，`as_of` 再按可用日取最新版本。

---

## 五、实测：前视偏差有多大

报告期 `20260630`（半年报）的公告日实测范围 **20260716 ~ 20260915**：

| 截面日 | 按报告期口径可见 | 按公告日口径（PIT）可见 |
|--------|----------------:|-----------------------:|
| 20260701 | 5225 只 | **0 只** |
| 20260801 | 5225 只 | 68 只（1.3%） |
| 20260901 | 5225 只 | 5214 只（99.8%） |
| 20260916 | 5225 只 | 5225 只（100%） |

**7 月 1 日那天，这份半年报一份都还没公告**（最早 7 月 16 日）。用报告期对齐，
回测会在 7 月 1 日一次性"读到" 5225 只股票 8 月底才披露的业绩 —— 这类偏差不会报错，
只会让净值曲线凭空变好看。

两期缓存面板（20260331 + 20260630，10449 条记录）的覆盖率：

| 截面日 | 覆盖率 | 说明 |
|--------|-------:|------|
| 20260430 | 77.8% | 一季报法定截止日当天，仍有一批在之后公告 |
| 20260701 | 99.3% | 一季报齐备、半年报未开始 |
| 20260901 | 99.9% | 半年报基本披露完 |
| 20261101 | 100% | — |

---

## 六、代码与用法

| 文件 | 职责 |
|------|------|
| `src/quant/fundamental_source.py` | AkShare 取数、字段规范化、股票池过滤、报告期序列 |
| `src/quant/pit.py` | PIT 面板（`as_of`/`as_of_panel`/`coverage`/`validate`）+ 本地缓存 + 增量同步 |
| `src/quant/price_panel.py` | QMT 前复权日线面板（按标的缓存 + manifest 区间增量） |
| `tests/unit/test_quant_pit.py` | 59 例：PIT 语义、快慢路径对拍、缓存增量、股票池过滤 |
| `tests/unit/test_quant_price_panel.py` | 13 例：OHLCV 组装、区间增量、失败记录、面板对齐 |

```python
from src.quant.fundamental_source import report_periods
from src.quant.pit import FundamentalStore
from src.quant.price_panel import PriceStore
import asyncio

# 1) 基本面：增量同步（已缓存的报告期自动跳过）
store = FundamentalStore("data/quant/fundamentals")
await store.sync(report_periods(2020, 2026))     # 自动剔除未披露报告期
panel = store.load_panel()
snapshot = panel.as_of("20260901")               # 只用 2026-09-01 当时已知的财报
print(panel.coverage("20260701"))                # 覆盖率，缺口不静默

# 2) 价格：QMT 前复权日线（经 qmt_guard 串行化 + 子进程隔离补下载）
prices = PriceStore("data/quant/prices")
await prices.sync(["600519", "300750"], "20200101", "20260915")
close = prices.load_panel(field="close")         # index=日期, columns=代码
```

**缓存布局**（`data/` 已 gitignore）：

```
data/quant/fundamentals/performance_20260630_a_share.csv.gz   # 按报告期 + 股票池命名
data/quant/prices/600519.csv.gz                               # 按标的
data/quant/prices/_manifest.json                              # 每只票已覆盖的日期区间
```

两个刻意的设计：

- 股票池写进文件名 —— 换池子（沪深A股 ↔ 全市场）后不会误用旧缓存，
  否则截面里会突然多出几千只新三板；
- 增量判断看 **manifest 里的区间**，而不是"文件存在与否" —— 否则
  「上次只拉了 2025、这次要 2020 起」会被误判命中，静默少 5 年数据。

**存储格式自适应**：装了 `pyarrow` 写 parquet，否则退回 CSV.gz（当前环境没有 pyarrow）。
想换 parquet 只需 `uv add pyarrow`，代码不用改。

---

## 七、Tushare 对照：本地已经有什么、真缺什么

依据 [Tushare 积分与频次权限对应表 doc 290](https://tushare.pro/document/2?doc_id=290)
与**本机逐接口实测**（2026-09-15）对照如下。

### 7.1 Tushare 要单独收费、而本地已经免费拿到的

| Tushare 独立权限 | 官方价 | 本地替代（实测） |
|------------------|-------:|------------------|
| 历史分钟 1/5/15/30/60m | 2000 元/年 | ✅ **QMT**（1m/5m/15m/30m/60m + 前复权日线） |
| 实时分钟 | 1000 元/月 | ✅ QMT `get_full_tick` 实时 tick |
| 实时日线 / 指数实时 / 申万实时 / ETF 实时 | 各 200 元/月 | ✅ 腾讯、新浪快照（本项目已接） |
| ETF 实时参考（IOPV/折价率） | 300 元/月 | ✅ `fund_etf_spot_em`（含 `IOPV实时估值`、`基金折价率`） |
| 美股日线 | 2000 元/年 | ✅ `stock_us_daily`（5763 行）+ 腾讯 `usNVDA` |
| 美股财报 | 500 元/年 | ✅ `stock_financial_us_report_em` |
| 港股财报 | 500 元/年 | ✅ `stock_financial_hk_report_em` |
| 公告信息（含 PDF 链接） | 1000 元/年 | ✅ `stock_notice_report`（标题/类型/日期/**网址**） |
| 新闻资讯 | 1000 元/年 | ✅ AkShare 新闻 + 项目 `news_fetcher` |
| 可转债价格变动 | 500 元/年 | ✅ `bond_zh_cov`（1053 行） |
| 券商研报库 | 500 元/年 | ✅ `stock_research_report_em`（含盈利预测） |
| 期货日线 | — | ✅ `futures_zh_daily_sina`（4244 行） |

**QMT 还额外给了三样 Tushare 没有的东西**（实测 `get_sector_list()` = 7419 个板块）：

- **精确股票池**：沪深A股 5220、沪深京A股 5598、上证A股 2318、深证A股 2902、
  创业板 1408、科创板 616、沪深ETF/基金/债券/转债/指数 —— 可用于把 AkShare 业绩报表里
  混进来的新三板剔干净（§3 陷阱 1 的根治办法）；
- **申万行业三级成分**：`SW1银行` = 42 只 …… 行业中性化不再需要外部行业表；
- **同花顺概念成分**：`TDGNCPO概念`、`TDGNAIGC概念` …… 做T模块的"关联板块"终于可以用
  **真实成分股**合成板块分时，而不必再用同业等权近似。

### 7.2 本地真正缺的（9 项，按对因子回测的重要性排序）

| # | 缺口 | 对应 Tushare | 实测证据 |
|---|------|--------------|----------|
| 1 | **资金流**（个股/板块大中小单净流入） | `moneyflow`（2000 积分） | `stock_individual_fund_flow`、`_rank`、`stock_market_fund_flow` **全部 ConnectionError**（东财域名被阻断） |
| 2 | **每日估值全字段**：PS/股息率TTM/换手率(自由流通)/量比/流通市值/自由流通股本 | `daily_basic`（2000 积分） | 本地只有 PE/PB（百度估值，项目已在用）+ 总市值（可从 QMT 股本推）；`stock_a_indicator_lg` 在当前 akshare 版本**已不存在** |
| 3 | **筹码分布 / 胜率** | 特色数据（10000 积分） | `stock_cyq_em` ConnectionError |
| 4 | **Tushare 口径的深度财务横截面**（100+ 指标、全市场按报告期一次取） | `fina_indicator_vip`（5000 积分） | 本地是东财业绩报表 10 指标 + 新浪逐股 86 列，字段口径混杂、逐股拉全市场不现实 |
| 5 | 历史集合竞价 | 单独 500 元/年 | QMT tick 只有当日，历史竞价无接口 |
| 6 | 盘前每日股本 | 单独 500 元/年 | 无 |
| 7 | 政策法规库 / 央行货币政策执行报告 | 1000 / 200 元/年 | 无（项目宏观连接器覆盖部分宏观指标，但非原文库） |
| 8 | 期权行情/分钟 | 2000 元/年 | `option_finance_board` 返回空 |
| 9 | 期货分钟 | 2000 元/年 | 仅日线可用；分钟取决于 QMT 是否开通期货行情权限 |

另有 1 项"接口在但坏了"（可自行修）：`stock_irm_cninfo`（互动易，对应 Tushare 500 元/年）
抛 `KeyError`，是 akshare 侧列名变更导致，可加适配层修掉。

### 7.3 费用：每年最低多少

价格取自 doc 290 表一，**积分按 1:10 购买（200 元 = 2000 积分），权限一年有效**；
微信专业群 1000 元送 5000 积分（比直接买 500 元贵，不划算）；机构价为个人价 ×10。

| 档位 | 年费 | 新增能力 | 对本项目的价值 |
|------|-----:|----------|----------------|
| **0 元** | 0 | 现状：QMT + AkShare | 价量因子、财务（10+15 字段）、行业/概念成分、公告、盈利预测、两融/北向/分红/预告**都已可得**；缺 §7.2 的 9 项 |
| **2000 积分** | **200 元** | `daily_basic`（按日一次取全市场 6000 行）+ `moneyflow` + `fina_indicator`（逐股） | **性价比最高**：一次补上缺口 1、2 —— 35 因子里价值类 6 个 + 流动性/规模类 9 个直接可算 |
| **5000 积分** | **500 元** | ＋`fina_indicator_vip`（全市场财务横截面）+ 频次 500/分 + 常规数据无上限 | **做全市场基本面因子的实际门槛**（补缺口 4）；建议的最低"完整档" |
| 10000 积分 | 1000 元 | ＋特色数据（筹码/胜率/盈利预测/券商金股） | 补缺口 3；但盈利预测本地已有替代，边际收益主要在筹码/胜率 |
| 15000 积分 | 1500 元 | 特色数据无总量限制 | 仅在需要全量特色数据时 |

**分钟数据不必买**（Tushare 2000 元/年 vs QMT 免费且能取前复权），
**新闻/公告/港美股财报/研报/可转债/ETF IOPV 也不必买**（本地都有替代）。

结论：**0 元能做完 M1～M3（价量+财务+行业+公告），200 元能补上最痛的估值与资金流，
500 元是把 35 个因子做全的建议档。**

### 7.4 500 元档采购清单（买什么、不买什么）

**要买的只有一项：5000 积分**（权限中心 → 积分购买/直付，500 元，1:10 比例）。
积分不是"勾选接口"，到账后自动解锁表一里所有门槛 ≤5000 分的接口。
**不要开任何"独立权限"**（表二那些是分钟/实时/新闻/公告/港美股，本地都有替代）。
微信专业群 1000 元送 5000 积分 —— 比直接买贵一倍，不划算。积分与权限**一年有效**，
机构价 = 个人价 × 10。

购买后实际要调用的接口（门槛标注，"★"为本次购买的核心价值）：

| 用途 | 接口 | 门槛 | 备注 |
|------|------|-----:|------|
| 估值+规模+流动性（价值/规模/流动性因子） | `daily_basic` | ★2000 | pe/pe_ttm/pb/ps/ps_ttm/dv_ratio/dv_ttm<br>turnover_rate(_f)/volume_ratio/free_share<br>total_mv/circ_mv + **limit_status(涨跌停状态)** |
| **全市场财务横截面**（成长/质量因子） | `fina_indicator_vip` | ★5000 | 100+ 字段，按报告期一次取全市场<br>（2000 档的 `fina_indicator` **只能逐股**，每次≤100 条，不现实） |
| 资金流因子 | `moneyflow` | ★2000 | 大/中/小/特大单净流入 |
| 回测可行性：涨停买不到/跌停卖不出 | `stk_limit` | ★2000 | 每日涨跌停价 |
| 回测可行性：停牌不可成交 | `suspend_d` | ★2000 | 每日停复牌 |
| 复权交叉校验 | `adj_factor` | 2000 | QMT 已给前复权，此项用于对拍 |
| 特色量价字段（可选） | `bak_daily` | 5000 | 内盘/外盘、振幅、强弱度、活跃度、笔换手、攻击波 |
| 指数估值（可选） | `index_dailybasic` | 2000 | 本地已有替代 |
| 名录与日历 | `stock_basic`、`trade_cal` | 免费 | QMT 板块名录可替代 |
| 未复权行情（基本用不上） | `daily`、`index_daily` | 免费 | QMT 已给**前复权**日线 |

**明确不买**（doc 290 表二，逐项都有本地替代，见 §7.1）：历史分钟 2000 元/年、
实时分钟 1000 元/月、实时日线/指数/申万/ETF 各 200 元/月、ETF 实时参考 300 元/月、
美股日线 2000 元/年、美股财报 500、港股财报 500、新闻 1000、公告 1000、可转债 500、
互动易 500（可用 akshare 修）、政策法规 1000、研报 500、央行报告 200、
期货历史分钟 2000/实时 1000 月、期权分钟 2000、申万分钟 2000。

只有两项"本地确实没有替代、但当前 35 因子用不上"：**盘前股本 500 元/年**
（隔夜股本变动因子才需要）、**集合竞价 500 元/年**（竞价类因子才需要）。

**历史回补耗时估算**（5000 档 = 500 次/分，常规数据无上限）：按 `trade_date` 循环，
`daily_basic` + `moneyflow` 各约 2430 次（10 年交易日）≈ 10 分钟；
`fina_indicator_vip` 按报告期 40 次 ≈ 1 分钟。全市场 10 年数据约 **15～20 分钟**拉完。

---

## 八、M1 验收状态
| 验收项 | 状态 | 证据 |
|--------|------|------|
| 任何回测日只用该日之前已公告的财报 | ✅ | `test_as_of_never_uses_future_announcement` 等 12 例；报告期口径对拍显示会提前 5225 只 |
| 无未来函数可**事后验证** | ✅ | `pit_leak_report(panel, dates)` → `{"clean": true, "leaks": 0}`，可写进回测报告 |
| 增量为真（不重复取数） | ✅ | `test_store_sync_is_incremental`、`test_sync_fetches_then_skips_covered_range` |
| 缺口不静默 | ✅ | 缺公告日的行记日志丢弃；失败报告期进 `failed_periods`；覆盖率随日期可查 |
| 前复权价格可得 | ✅ | QMT `stock_close:{code}`（`adjust="qfq"`），经 `qmt_guard` 串行化 + 子进程隔离下载 |

**已知边界（M2 前必须记住）**：

- 基本面字段目前是「业绩报表口径」的 10 个指标（eps/revenue/revenue_yoy/net_profit/
  net_profit_yoy/bps/roe/ocfps/gross_margin/industry）+ 新浪指标 15 个补充字段，
  离设计文档的 35 个因子还差"合成"这一步（M2）；
- 不含北交所；`industry` 是申万/东财混合口径，中性化前需要统一行业分类（M2）；
- 财报的 `最新公告日期` 是"最近一次公告日"：若财报被更正，公告日会晚于首次披露 ——
  这是**偏保守**的方向（宁可用得晚，不用得早），可以接受；
- 价格是**前复权**：复权因子会随每次分红送转重算历史，因此「今天拉的历史」与
  「半年前拉的历史」价格不一致。做因子（收益率/波动率）没问题，
  但**任何绝对价格相关的计算都不要落库固化**。

---

## 九、全量历史下载实测（2026-09-16，Tushare 5000 积分档）

一次跑完 2006-01-01 ~ 2026-09-15 的全部数据集：

| 数据集 | 起始 | 分区 | 行数 |
|--------|------|-----:|-----:|
| daily | 2006-01-01 | 5030 | 14,497,474 |
| daily_basic | 2006-01-01 | 5030 | 14,497,305 |
| adj_factor | 2006-01-01 | 5030 | 15,051,481 |
| moneyflow | 2010-01-04 | 4057 | 13,135,854 |
| stk_limit | 2010-01-04 | 4057 | 13,537,201 |
| suspend_d | 2006-01-01 | 5030 | 527,626 |
| bak_daily | 2018-01-01 | 2113 | 8,573,250 |
| index_daily | 2005-01-04 | 5272 | 20,964 |
| fina_indicator_vip | 季度 | 106 | — |
| **合计** | | | **79,841,155** |

耗时约 3 小时（含限流等待），失败分区 16 个。

### 9.1 那 16 个失败分区：**是 Tushare 源侧缺数，不是下载问题**

16 天全部落在 `bak_daily`，接口返回空表。**用交易日历核对过：这 16 天都是交易日**，
并且用正确的调用签名重试了一次，仍然全部返回空表：

```
20190401 20191024 20191104 20191128 20200220 20200225 20200316 20200423
20200618 20200708 20200720 20200824 20201120 20210129 20210203 20210316
```

影响面：`bak_daily` 供给 11 个备用行情因子（swing/selling/buying/strength/activity/
avg_turnover/attack/interval_3/interval_6/bak_vol_ratio/bak_turnover）。
缺失 16/2113 天 = **0.76%**，这些天上相关因子的值是 NaN。
由于 DSL 用三值逻辑（未知 → 不入选），**不会产生错误决策，只是那几天少一些信号**。

### 9.2 全量入库后的仓库规模

| 表 | 行数 |
|----|-----:|
| quant_daily | 15,384,650 |
| quant_daily_basic | 15,384,481 |
| quant_adj_factor | 15,941,443 |
| quant_moneyflow | 13,135,854 |
| quant_stk_limit | 13,537,201 |
| quant_suspend_d | 527,626 |
| quant_bak_daily | 8,573,250 |
| quant_index_daily | 20,964 |
| **合计** | **约 7,900 万行** |

入库是**幂等**的：重复执行不会产生重复行（去重键 + 方言原生 UPSERT），
所以"下载补数后重跑一遍 ingest"是安全操作，不需要先清理。

---

## 八、量化选股报「数据滞后」：根因、三个 bug、以及自动同步与启动自检（2026-09-23）

### 8.1 现象

量化选股页面报：

> ⚠ 数据滞后：本次用的是 **20260917** 的行情，最近一个已收盘交易日是 **20260922**。
> 本地行情仓库没有同步到最新 …… 按「▶ 立即选股」重跑不会变新。

### 8.2 根因 A：数据**下载了但没灌库**（`stk_limit` 卡住整条链）

逐层查下来的事实：

| 层 | `stk_limit` 的日期 |
|---|---|
| 下载分区（CSV） | **20260922** ← 早就是新的 |
| 本地仓库（SQLite） | **20260917** ← 差 5 天 |

选股的涨停板/情绪特征要读 `stk_limit`（`moss_selector/adapters.py` 用
`daily` + `stk_limit` 现算 `is_limit_up`），于是整条链被它一个人卡在 0917。

为什么没灌进去：`quant_data_sync` 的**早返回分支**用 `DatasetStore("daily").keys()`
算出 `pending`，再把这个 `pending` **喂给每一个数据集** —— 默认了"所有数据集水位一致"。
而 `daily` 当时已经是最新的，于是 `pending` 为空，`stk_limit` 自己的那几天
**永远补不上**；作业每天如实记「已是最新」，分区文件就躺在磁盘上没人灌。
（同一个 bug 的完整分析与修法见 `docs/MAINLINE_MINING.md` §16.73：
补灌改成 `_ingest_gaps()`，**每档用自己的水位去截自己的分区**。）

### 8.3 根因 B：11 个文件用了 `brief` 却没导入它 —— **真实异常被遮蔽**

修完数据后立刻暴露的问题：选股仍然失败，`last_error` 是

```
选股子进程失败：NameError: name 'brief' is not defined
```

`moss_selector/` 的 6 个模块（`adapters / main / selector / utils / model_trainer /
moss_integrator`）以及 `scripts/` 的 5 个脚本，都是
`from src.core.errors import BRIEF_DEFAULT` —— **只导入了常量，没导入函数**，
但正文里到处在调 `brief(exc, BRIEF_DEFAULT)`。

后果比"少个日志"严重得多：这些 `brief(...)` 全都在 `except` 分支里，
所以**任何一次真实异常都会在格式化异常消息时再抛一个 NameError**，
把原始错误彻底盖掉 —— 排查时看到的是"brief 未定义"，而不是"Tushare 取数失败"
或"仓库不可用"。这次的 20260917 之外，之前若干次"查不出原因"很可能都栽在这里。

修法：给 11 个文件补上 `brief`。静态排查脚本（一次性）按 AST 收集绑定名，
找出"用了 `brief(` 但本文件没有绑定 `brief`"的文件；修完后只剩
`src/domain/alerts/prompts.py` 的 `_event_brief`（同名子串，不是同一个东西）。

### 8.4 根因 C：同步作业覆盖的数据集**少于面板真正会读的**

作业原来只同步 4 个：`daily / daily_basic / stk_limit / moneyflow`。
而 `src/quant/panels.py` 实际还要读：

| 数据集 | 用途 | 缺了会怎样 |
|---|---|---|
| `adj_factor` | 复权因子（`close × adj_factor`） | **静默降级**：价格不复权，除权日出现假跳空 |
| `index_daily` | 相对强度 RS 的基准 | 相对强度算不出 / 用旧基准 |
| `suspend_d` | 停牌（不可成交） | 停牌股被当成可交易 |
| `bak_daily` | 内盘/外盘、振幅等特色字段 | 相关因子空值 |

实测这几个当时确实落后（`adj_factor` 0915、`index_daily` 0917、`suspend_d` 0915、
`bak_daily` 0915）—— 而它们**不在作业清单里，就不会被自动补上**。

修法：清单与 `sync_gap.DAILY_DATASETS` **同源**（一处定义、两处引用），
并加回归测试断言 `JOB_REGISTRY["quant_data_sync"].params["datasets"] == DAILY_DATASETS`。

### 8.5 新增：显式的「同步缺口」判定

以前健康度页面**分别**报了分区覆盖与仓库水位两列，但**从来没有互相比较过**，
也没和「应该是哪一天」比较 —— 三个数字各自看都正常，缺口只在它们之间。

新增 `src/quant/sync_gap.py`（**纯函数、零 I/O**：输入就是那两份已有落盘缓存的统计），
对每个日频数据集给出四态之一：

| 状态 | 含义 | 该做什么 |
|---|---|---|
| `ok` | 仓库已到应有日期 | 什么都不用做 |
| `need_ingest` | **分区比仓库新** | 只需灌库（**本次事故就是这一态**） |
| `need_download` | 分区与仓库都落后 | 先联网下载 |
| `unknown` | 缺统计，判不了 | **不猜**，也不当作已同步 |

⚠️ `synced` 的语义是「**已确认**全部同步」而不是「没发现落后」——
这个区别在"全都判定不了"时会分叉。第一版写成 `not behind` 就报 `synced=True`，
**是写完测试才发现的**：缺统计时启动自检会认为一切正常、连补偿都不做。

判定结果挂在 `/api/v1/health` 的 `data_sources.health.sync_gap`。

### 8.6 新增：服务启动自动检查并自动补

`src/api/main.py` 的 lifespan 里新增后台任务 `quant-sync-startup-check`：
启动 30 秒后比对三层水位，**缺了就自动触发一次 `quant_data_sync`**（`trigger="startup"`）。

三条约束（都不是可选的）：

1. **延迟 + 后台线程**：数据健康度要遍历 3.5 万个分区目录（磁盘忙时实测 140 秒），
   绝不能占着首屏与 `/health`；
2. **判定只读已有统计**，不新增任何遍历；
3. **任何异常只记日志** —— 自检不是服务启动的前置条件。

`CronScheduler.trigger()` 是为此新增的公开入口，与定时触发的差别只有两点：
运行记录里的 `trigger` 字段不同；**不受"连续失败自动暂停"拦截**
（暂停是防"每 30 分钟撞同一堵墙"，而启动是一次性的，重启本身可能已消除失败原因）。

### 8.7 新增：同步作业改成**重试节奏**（这才叫"实时自动同步"）

原来 `40 16 * * 1-5` —— 一天一次。那次若撞上 Tushare 还没发布
（各数据集发布时刻不同，`moneyflow` 实测比 `daily` 晚约 2 个交易日），
就只能等**下一个工作日**，中间用户的页面一直显示「数据滞后」而不自愈。

现在 `*/30 16-23 * * 1-5`：工作日 16:00~23:30 每 30 分钟一次。
作业幂等（没有缺口时只查几次水位就返回），所以提高频率代价很小。
⚠️ 16:00 之前的轮次基本空转：`latest_complete_trade_date()` 的发布线是
**16:30**（`freshness.EOD_RELEASE`），早于它"应该有的最新一天"还是昨天 ——
所以区间从 16 点起，而不是更早。

### 8.8 「自检到底跑没跑」必须可观测（否则等于没做）

自检成功时只能打 `logger.info`，而 **uvicorn 默认让应用侧 logger 停在 WARNING**
—— 于是"启动时查过、结论是已同步"在日志里**根本看不到**，
"没消息"与"没运行"分不清（这一条是我自己踩的：修完后无法自证自检跑过）。

所以把最近一次结论记进 `data/quant/sync_check.json` 并挂到健康度载荷的
`sync_gap.startup_check` 上。**落盘而不是只放内存**，是因为内存版有两个洞：
进程外看不到；且 `warm_data_health` 启动时就把载荷缓存 5 分钟，
而自检延迟 30 秒才写记录 → 那 5 分钟内 `/health` 拿到的永远是空的，看着像"没跑"。

实测留下的记录：

```json
{ "checked_at": "2026-09-23 08:55:58", "expected": "20260922",
  "synced": true, "behind": [], "unknown": [], "action": "none" }
```

以及更早一次（08:54:51）**真的检测到缺口并触发了补偿**：
`{"synced": false, "action": "已触发补偿同步"}`。

### 8.9 ⚠️ 一个必须说清的边界：判定读的是**缓存统计**

`sync_gap` 的两份输入是落盘缓存（`tushare_stats.json` 30 分钟、
`warehouse_stats.json` 10 分钟），所以判定用的日期**可能与真实值相差一个缓存周期**。
实测就出现过一次：08:54 那次自检读到的是修数据**之前**的缓存，于是报了
`synced=false` 并触发补偿 —— 而实际数据已是最新。

这是**可接受的假阳性**：补偿作业幂等，跑一次只多花几秒；而反过来
（缓存说"已同步"、实际落后）不会发生，因为落盘缓存只会比真实值**旧**。
但**不能**拿它当"此刻一定如此"的证据 —— 要确认当前状态就直接查仓库水位。

### 8.10 验证

| 检查 | 结果 |
|---|---|
| 8 个日频数据集 分区 / 仓库 | 全部 **20260922 / 20260922** |
| `/health` 的 `sync_gap` | `synced=True`、`behind=[]`、`unknown=[]` |
| 启动自检记录 | `data/quant/sync_check.json` 有 `checked_at`、`synced`、`action` |
| 量化选股 | `last_trade_date=20260922`、`last_selected=10`、**`last_error` 为空** |
| 回归测试 | `tests/unit/test_quant_sync_gap.py` 21 个 + `test_quant_freshness.py`（含 cron 与数据集清单断言） |

**本轮第 16 次推翻预期**：以为"数据补上就好了"，实际补完数据才暴露出
`brief` 未导入这个**一直在遮蔽真实异常**的 bug；如果没有先修数据、
让选股真的跑一次，它会继续表现为"某个说不清的 NameError"。

### 8.11 ⚠️ 顺带挖出的第三个坑：**跑测试会写坏正在运行的服务的缓存**

`sync_gap` 上线后自测时，8 个数据集**全部变成 `unknown`** —— 而仓库明明是好的。
顺着 `warehouse_stats.json` 摸下去，内容是这样的：

```json
{ "available": false, "dialect": "mysql",
  "description": "环境变量 MOSS_DB_URL：mysql+pymysql://nobody:***@127.0.0.1:1/none",
  "hint": "设置 MOSS_MYSQL_HOST/USER/PASSWORD（或 MOSS_DB_URL）后切换到该数据库" }
```

`nobody@127.0.0.1:1/none` 是**测试夹具**里的假地址。文件写入时间
（09:01:48）与那次 `pytest tests/unit` 的结束时间**完全吻合**。
罪魁是 `test_data_health_survives_broken_warehouse`：

1. `monkeypatch.setenv("MOSS_DB_URL", "mysql+pymysql://nobody:nopass@127.0.0.1:1/none")`
2. `invalidate_cache(include_disk=True)` —— **删掉生产的** `warehouse_stats.json`
3. `build_data_health(force=True)` —— 把「仓库不可用(mysql)」**写进同一个生产路径**

**后果不是「测试脏了」，而是正在运行的服务被带坏**：跑一次测试，服务在接下来
最多 10 分钟里把本地 14GB 的 SQLite 仓库报成「MySQL 不可用」，
选股、健康度面板、同步缺口判定全部据此降级 —— 静默，而且**看起来像环境坏了**
（这一条正是先看到 `sync_gap` 全 `unknown` 才顺藤摸到的）。

**修法**（两层，缺一不可）：

1. `data_health` 的两个落盘缓存路径改为**可重定向**
   （`MOSS_HEALTH_CACHE_DIR` + `_warehouse_stats_file()` / `_tushare_stats_file()`）；
2. `tests/conftest.py` 加一条 **autouse** fixture，把缓存目录指向 `tmp_dir`。

为什么必须做成 autouse、而不是只修那一个用例：**将来任何新增测试**只要碰
`build_data_health(force=True)` 都会写这个文件，「记得自己清理」是防不住的。

验证（跑完 `test_quant_warehouse.py` 32 个用例后比对生产缓存哈希）：

```
跑测试前 hash: 066A8682F3B8E1AD304061CAB863F94BF5318E6B8E8853C272C91ABC59168E3E
跑测试后 hash: 066A8682F3B8E1AD304061CAB863F94BF5318E6B8E8853C272C91ABC59168E3E
✅ 生产缓存未被测试改动
```

**第 17 次推翻预期**：以为「测试污染」只是测试之间的事，实际它会打到**生产进程**上。
另外还暴露一处设计事实：健康度载荷自身有 300 秒内存缓存，
所以清掉落盘缓存后**接口最长要 5 分钟才反映真实状态**（实测等到第 7 次轮询）
—— 这个延迟本身可接受（面板是分钟级信息），但排查时要知道它存在，
否则很容易误判成「修了没生效」。

### 8.12 ⚠️ 用户问「今天已经收盘了，为什么情绪周期图上还没有今天？」（2026-09-23 晚）

现象：18:49 用户在竞价选股面板追问 —— 今天（09-23，周三）已收盘，
情绪周期五线图最后一个点仍是 **09-22**。查下来是**两处独立缺陷叠加**，
而且都不是选股/画图逻辑的问题。

#### 根因 ①：目标日被**陈旧的交易日历缓存**锁死，作业天天报「已是最新」

链路：`quant_data_sync` → `freshness.latest_complete_trade_date()`
→ `freshness.calendar_days()` → **本地 `trade_cal` 分区缓存**（刻意不联网）。

实测那天缓存窗口是 `19901219 ~ 20260922`（`_manifest.json` 的 `start/end`），
于是 16:30（`EOD_RELEASE`）之后的任何时刻 `target` 都等于 **20260922**，
与水位相等 → 走早返回分支：

```
2026-09-23T17:30:02  quant_data_sync  success  rows=0  dur=51.6s
2026-09-23T18:00:02  quant_data_sync  success  rows=0  dur=54.0s
2026-09-23T18:30:02  quant_data_sync  success  rows=0  dur=53.9s
```

三次「成功」、零行、每次 50 多秒（只跑了补灌扫描）。而同一时刻
Tushare **已经发布**了当日数据（18:49 手测 `daily(trade_date=20260923)`
返回 **5556 行**，`trade_cal` 也确认 09-23 `is_open=1`）。

**它自己解不开**：`download.py::calendar(start, end)` 只在
`end > 缓存末日` 时才重新拉取，而这个 `end` 正是从缓存里算出来的
—— **循环依赖**。缓存只能靠"某次显式传了更晚 end 的下载"被动推进
（那天早上 08:45 那次就是这样把缓存推到 0922 的）。

修法：`_quant_data_sync` 开头新增 `_refresh_calendar_horizon()` ——
用**墙上时钟**（今天）去顶一次日历，显式制造 `end > 缓存末日`，
把这个环打断；刷新失败/空表只记一句说明，按旧日历继续（不猜、也不判失败）。

#### 根因 ②：`index_daily` 走错入口，异常把整轮同步打断在**灌库之前**

作业清单按 `sync_gap.DAILY_DATASETS` 列了 8 档（8.4 节那次改的），
但 `index_daily` **不在** `download.py::DAILY_DATASETS` 里
—— 指数是 5 个代码逐个拉的，入口是 `sync_index`。于是：

```
TushareDownloader.sync_daily("index_daily", days) → KeyError: 'index_daily'
失败：下载 20260922~20260923 失败 KeyError: 'index_daily'   (18:52:31 manual)
```

更糟的是它抛在循环里、`_ingest_gaps()` **之前**：`daily/daily_basic/
adj_factor/stk_limit/moneyflow` 的分区**已经下好**，仓库却一行没进，
作业记录只有一句"下载失败"（这个 KeyError 一直潜伏到今天第一次真的发起下载）。

修法：`_download_one()` 按数据集分流（`index_daily` → `sync_index`）+
**逐档隔离**（单档异常记名继续，全部失败才判 failed）。

#### 验证

| 检查 | 结果 |
|---|---|
| `trade_cal` 缓存 | `19901219 ~ 20260923`（8732 天），`latest_complete_trade_date()` = **20260923** |
| `/health` 的 `sync_gap` | 刷新日历后立刻从 `synced=true / behind=[]` 变成 `synced=false`、8 档全 `need_download`（证明原来那句"已同步"是**瞎的**） |
| 09-23 分区（8 档） | 全部落盘：daily 5210 / daily_basic 5210 / adj_factor 5222 / stk_limit 5222 / moneyflow 5210 / index_daily 5 / suspend_d 13 / bak_daily 5226 |
| 仓库 09-23 | `quant_daily` 5210、`quant_stk_limit` 5222（情绪周期只用这两张 + `quant_stock_basic`） |
| 情绪周期接口 | `days` 末值 = **20260923**；小周期 4、大周期 6、大肉 50、大面 15、连板数 12 |
| 自愈链路 | 新代码实例启动后：`18:58:00 quant_data_sync startup success`（75s，启动自检发现缺口 → 补偿）→ 8 档全部灌进仓库；`19:00:02 schedule success`（54.6s，已是无操作） |
| 回归测试 | `test_quant_freshness.py` 新增 4 个（`sync_index` 分流 / 逐档隔离 / 日历推进 / 缓存已新则不联网 / 刷新失败不判失败），29 个全过 |

> 两个根因**都不是**"Tushare 没发布"：18:49 实测 `daily(trade_date=20260923)`
> 返回 5556 行、`trade_cal` 也确认 `is_open=1` —— 数据早就在那儿，
> 是本地两层判定把自己锁住了（一个锁目标日、一个锁执行路径）。

#### 排查这一类问题的**最短路径**（别再从头猜）

1. 先分清楚是哪一层：**分区**（`data/quant/tushare/a_share/<dataset>/<date>.parquet`）、
   **仓库**（`warehouse.db` 各表 `max(trade_date)`）、**应该有的那天**
   （`latest_complete_trade_date()`）。三者一比就知道卡在哪一步；
2. 再问"作业到底报了什么"：`data/<env>/scheduler/runs.jsonl`
   （`status/records_processed/duration_ms`）—— **`success` + `rows=0` + 50 秒**
   就是"判定成已最新"的指纹；真有缺口时 duration 会是分钟级；
3. 上游是否有数据：直接 `TushareClient().call("daily", trade_date=今天)`
   看行数，别靠猜"是不是没发布"。

### 8.13 ⚠️ 附带发现：`data/scheduler/job_runs.jsonl` 不是当前服务的运行记录

`data/scheduler/` 是**非 dev 环境**的调度目录；`--env dev` 会把
`SCHEDULER_DIR` 改道到 `data/dev/scheduler/`。本次排查时先读了
`data/scheduler/job_runs.jsonl`（最后一条停在 2026-09-14），差点得出
"作业根本没跑"的错误结论 —— 而那天的作业记录全在
`data/dev/scheduler/runs.jsonl`。**看运行记录先确认服务的 `MOSS_ENV`。**
