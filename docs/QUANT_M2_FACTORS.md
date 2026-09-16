# 多因子回测 M2：Tushare 数据接入 + 35 因子做全

> 承接 [`QUANT_M1_DATA_LAYER.md`](QUANT_M1_DATA_LAYER.md)（PIT 数据层）。
> 本文记录 M2 的交付：Tushare 5000 积分档接入、下载/存储链路、35 个核心因子，
> 以及**真实数据验证中挖出的 6 个缺陷**（全部已修，都带复现证据）。
> 所有数字均为 2026-09-15 本机实测。

---

## 一、Tushare 权限实测：5000 积分档已到账

`python scripts/quant_sync.py doctor` 逐个接口小样本探测（11/11 可用）：

| 接口 | 门槛 | 实测行数 | 用途 |
|------|-----:|---------:|------|
| `stock_basic` | 免费 | 5562 | 名录/行业/上市日期 |
| `trade_cal` | 免费 | — | 交易日历（决定要拉哪些分区） |
| `daily` | 免费 | 5548/日 | 成交额（Amihud）、涨跌幅 |
| `daily_basic` | 2000 | 5548/日 | 估值/市值/股本/换手/量比/涨跌停状态 |
| `adj_factor` | 2000 | 5562/日 | 复权因子 |
| `stk_limit` | 2000 | 5641/日 | 涨跌停价 |
| `suspend_d` | 2000 | 按日 | 停复牌 |
| `moneyflow` | 2000 | 5548/日 | 资金净流入 |
| `index_dailybasic` | 2000 | 12/日 | 指数估值（本地已有替代） |
| **`bak_daily`** | **5000** | 5571/日 | 内盘/外盘、振幅、活跃度等特色字段 |
| **`fina_indicator_vip`** | **5000** | 6883/期 | **全市场财务横截面（PIT 骨架）** |

token 解析做了**三级兜底**（实测踩坑）：用户把 token 写进 Windows 环境变量后，
正在运行的服务进程**看不到**它（子进程继承的是父进程启动时的环境快照）。
注册表 User/Machine 都写好了、`os.environ` 却是空的。因此解析顺序为：

```
环境变量（大小写都试） → 项目 .env → Windows 注册表（winreg）
```

第三级让"设置完环境变量但没重启进程"也能直接跑通。token 全程不打印（只报首尾 4 位）。

---

## 二、下载与存储

### 2.1 落盘布局

```
data/quant/tushare/a_share/daily/20260914.csv.gz          # 一个交易日一张全市场横截面
data/quant/tushare/a_share/daily_basic/20260914.csv.gz
data/quant/tushare/a_share/adj_factor/20260914.csv.gz
data/quant/tushare/a_share/moneyflow/20260914.csv.gz
data/quant/tushare/a_share/stk_limit/20260914.csv.gz
data/quant/tushare/a_share/suspend_d/20260914.csv.gz
data/quant/tushare/a_share/bak_daily/20260914.csv.gz
data/quant/tushare/a_share/index_daily/20260914.csv.gz
data/quant/tushare/a_share/fina_indicator_vip/20260630.csv.gz   # 一个报告期一张
data/quant/tushare/a_share/trade_cal/static.csv.gz
data/quant/tushare/a_share/stock_basic/static.csv.gz
data/quant/tushare/a_share/<dataset>/_manifest.json            # 分区 → 行数/列/覆盖区间
```

四条设计约束（与 M1 的 store 同一套哲学）：

1. **一个分区一个文件** → 断点续传粒度就是"一天"，重跑只补缺的那几天；
2. **命中判断看 manifest 的覆盖区间**，不看"文件是否存在"；
3. **股票池写进路径**（`a_share` / `all`），换池子不会误用旧缓存；
4. 格式自适应：装了 pyarrow 写 parquet，否则 CSV.gz（当前环境无 pyarrow）。

### 2.2 实测规模（2026 全年至今）

```
$ python scripts/quant_sync.py status
  adj_factor          分区 171  行数 889962   20260105~20260915
  bak_daily           分区 171  行数 889599   20260105~20260915
  daily               分区 171  行数 887176   20260105~20260915
  daily_basic         分区 171  行数 887176   20260105~20260915
  fina_indicator_vip  分区   6  行数  42279   20250331~20260630
  index_daily         分区 171  行数    855   20260105~20260915
  moneyflow           分区 171  行数 887176   20260105~20260915
  stk_limit           分区 171  行数 889109   20260105~20260915
  stock_basic         分区   1  行数   5562   static
  suspend_d           分区 171  行数   2381   20260105~20260915
  trade_cal           分区   1  行数    171   static
```

全量下载 **498.9 万行 / 0 失败**，耗时约 6 分钟（限流 450 次/分）。

### 2.3 单位规范（不统一就是 10000 倍级错误）

| 类别 | Tushare 原口径 | 落盘口径 |
|------|----------------|----------|
| `daily.amount` | 千元 | **元** |
| `daily_basic.total_mv` / `circ_mv` | 万元 | **元** |
| `daily_basic.total_share` / `float_share` / `free_share` | 万股 | **股** |
| `moneyflow.net_mf_amount` | 万元 | **元** |
| `fina_indicator.fcff` | 元 | 元 |
| 各类比率（roe/毛利率/增速/换手率…） | 百分数 | 百分数（18.5 = 18.5%） |

---

## 三、35 个核心因子（七大类，全部面板化）

| 类别 | 数量 | 因子键 |
|------|-----:|--------|
| 价值 | 6 | `ep`、`bp`、`sp`、`cfp`、`dividend_yield`、`peg` |
| 成长 | 5 | `revenue_growth`、`profit_growth`、`roe_growth`、`gross_margin_trend`、`ocf_growth` |
| 质量 | 6 | `roe`、`roa`、`gross_margin`、`net_margin`、`low_leverage`、`ocf_to_profit` |
| 动量 | 5 | `momentum_20`、`momentum_60`、`momentum_120`、`relative_strength`、`reversal_5` |
| 波动率 | 4 | `volatility_20`、`volatility_60`、`atr_20`、`downside_vol_20` |
| 流动性 | 5 | `turnover_rate`、`amount`、`amihud`、`volume_ratio`、`money_flow_ratio` |
| 规模 | 4 | `total_mv`、`circ_mv`、`log_mv`、`free_float_mv` |

三条不可让步的规则（每条都有对应用例）：

1. **方向统一**：全部调成"值越大越看好"（`direction` 记录原始方向）。
   例如 PEG 取反、资产负债率取反、市值取反 —— 否则 IC 的正负号会在因子之间串味；
2. **只用过去**：滚动窗口 `rolling(...)`；财务走 PIT（按公告日 + 1 天滞后）；
3. **不做前向填充**：缺数据保持 NaN，绝不 ffill 到未来（否则"停牌还能成交"）。

### 3.1 真实数据覆盖率（171 交易日 × 5238 只）

| 因子 | 全样本覆盖 | 末端覆盖只数 | 说明 |
|------|-----------:|-------------:|------|
| `low_leverage` | 99.95% | 5238 | |
| `ocf_growth` / `profit_growth` | 99.7% | 5238 | |
| `roe` / `roa` / `net_margin` | 98~99% | 5136~5236 | |
| `momentum_20` | 87.2% | 5190 | 前 20 天无窗口 |
| `volatility_60` | 81.7% | 5204 | 前 60 天无窗口 |
| `ocf_to_profit` | 74.6% | 3878 | 原数据 6951/9611 非空 |
| `ep` | 70.8% | 3633 | 亏损股 PE 为空（真实情况） |
| `dividend_yield` | 66.1% | 3462 | 不分红股为空（真实情况） |
| `peg` | 40.1% | 2035 | 需正增速（真实情况） |
| `momentum_120` | 29.4% | 5153 | 只有后 51 天有窗口 |

**覆盖率低的因子不一定是坏因子** —— `ep`/`dividend_yield`/`peg` 的低覆盖来自 A 股
本身（亏损、不分红、负增长），这也是它们天然带"质量筛选"属性的原因。

### 3.2 真实数据 IC 检验（前瞻 20 日，日频截面，171 天）

```
$ python scripts/quant_sync.py ic --start 2026-01-01 --end 2026-09-15 --horizon 20
            factor   category      IC    ICIR     t  IC>0  periods
      momentum_120   momentum -0.2083 -1.1069 -6.16 0.097       31
            amount  liquidity -0.1093 -0.5736 -7.05 0.377      151
        ocf_growth     growth  0.0131  0.4737  5.82 0.669      151
     ocf_to_profit    quality  0.0213  0.4586  5.64 0.682      151
     turnover_rate  liquidity -0.0938 -0.4140 -5.09 0.371      151
            amihud  liquidity -0.0670 -0.4111 -4.88 0.333      141
       momentum_60   momentum -0.1050 -0.3725 -3.55 0.495       91
 relative_strength   momentum -0.1050 -0.3725 -3.55 0.495       91
       free_float_mv      size  0.0593  0.3437  4.22 0.649      151
        volatility_20 volatility  0.0878  0.3304  3.92 0.582      141
             total_mv      size  0.0595  0.3203  3.94 0.623      151
                   bp     value  0.0647  0.2884  3.54 0.576      151
                   ep     value  0.0495  0.2245  2.76 0.603      151
                   …（其余见 data/quant/factor_panel/_ic_20d.csv）
```

**必须诚实标注的三件事**：

1. **t 值被系统性高估**：日频截面 + 20 日前瞻收益 → 相邻交易日的 IC 高度重叠
   （151 个"样本"实际只有约 151/20 ≈ 8 个独立观测）。这里只适合"筛掉明显无效的因子"，
   不能当显著性结论。正式评估要月频调仓 + walk-forward（M3）。
2. **样本只有 171 个交易日（2026 年至今）**，不能外推为长期规律。
3. 结果是**自洽**的，可作交叉验证：动量类全为负 IC（这段窗口是反转行情）、
   `reversal_5` 为正、小市值（`total_mv` 取反后正 IC）与低流动性（`amount` 负 IC）溢价、
   现金流质量（`ocf_to_profit`、`ocf_growth`）为正 —— 三组符号互相印证。

**一个值得记下的发现**：`relative_strength` 与 `momentum_60` 的 **IC 完全相同**
（−0.1050 / t=−3.55）。原因：RS = 个股 60 日收益 − 沪深300 同期收益，
而基准在同一天对所有股票是**同一个常数**，扣减不改变截面排序。
→ 二者只能取其一对截面选股有用（这正是 M3 聚类去重该做的事）；
RS 的价值在时序维度（判断"今天是不是普涨"），不在截面排名。

---

## 四、真实数据验证中挖出并修掉的 6 个缺陷

| # | 缺陷 | 症状 | 根因与修复 |
|---|------|------|------------|
| 1 | **交易日历缓存静默缩窄区间** | 请求 2026 全年，只下了 11 天却报"成功" | 日历缓存成单个 static 分区、没记覆盖区间 → 请求更宽区间时按缓存内容返回。修：manifest 记录 start/end，不够就重新拉取并扩展 |
| 2 | **Tushare 只返回默认字段** | `ocf_to_profit` 因子整列空值 | 文档里标"默认显示=N"的字段必须显式传 `fields=` → 修：显式列出全部所需字段（修复后 6951/9611 非空） |
| 3 | **未复权价 → 除权日假跳空** | 10 送 10 当天动量 −50% | `daily` 是未复权价 → 修：用 `adj_factor` 转后复权（`close × adj_factor`）；同时保留 `close_raw` 供市值类因子用 |
| 4 | **同比查找键错位** | "毛利率同比变化"恒等于 0（比 NaN 更隐蔽） | 索引建在"P−1年"标签上 → 查 P−1年 时拿到的是 P 自己的值 → 修：索引建在真实报告期，查询时才做年份平移 |
| 5 | **修正公告导致索引非唯一** | "毛利率同比变化"整列 NaN（异常被兜住，静默无值） | 同一 (code, 报告期) 有多条公告日版本 → `reindex` 抛 non-unique multi-index → 修：建索引前去重，保留可用日最晚的版本 |
| 6 | **公告日早于报告期 = 未来信息** | Tushare 里确有此行（603400 报告期 20260630、公告日 20260422） | 报告期未结束就看到它 = 未来信息 → 修：`PitPanel` 直接丢弃并计数 + 记日志 |

第 4、5 条的教训值得强调：**因子算错了却不报错**（恒 0 或整列 NaN）比崩溃危险得多，
所以每个因子都必须有"构造对照样本 → 断言期望值"的用例，而不是只断言"不报错"。

---

## 五、代码清单与用法

| 文件 | 职责 |
|------|------|
| `src/quant/tushare_source.py` | token 三级解析、限流（线程安全）、错误分类、权限自检、字段/单位规范化 |
| `src/quant/dataset_store.py` | 分区存储（manifest 记覆盖区间）+ 增量同步 |
| `src/quant/download.py` | 接口 → 规范化 → 落盘；交易日历/指数/财务横截面一键下载 |
| `src/quant/panels.py` | 分区缓存 → 面板（含复权）+ PIT 财务装配 + 停牌集合 |
| `src/quant/factor_library_v2.py` | **35 个因子** + 覆盖率报表 + 截面取用 |
| `scripts/quant_sync.py` | CLI：`doctor` / `download` / `factors` / `ic` / `status` |
| 测试 | `test_tushare_source.py`(36) + `test_factor_library_v2.py`(23) + `test_quant_panels.py`(8) |

```bash
# 0) 权限自检（买完 Tushare 第一件事）
python scripts/quant_sync.py doctor

# 1) 增量下载（已缓存的分区自动跳过；--max-days 用于冒烟）
python scripts/quant_sync.py download --start 2026-01-01 --end 2026-09-15 \
    --fina --start-year 2025 --end-year 2026

# 2) 35 因子 + 覆盖率报表（读缓存，不联网）
python scripts/quant_sync.py factors --start 2026-01-01 --end 2026-09-15 \
    --out data/quant/factor_panel

# 3) IC/ICIR 快速筛选
python scripts/quant_sync.py ic --start 2026-01-01 --end 2026-09-15 --horizon 20

# 4) 缓存体检
python scripts/quant_sync.py status
```

---

## 六、已知边界与下一步

**当前边界**：

- 样本只有 2026 年至今（171 个交易日）；要跨牛熊需回补 2015 年以来的数据
  （`--start 2015-01-01`，按当前速率约 15~20 分钟）。
- 财务横截面只有 6 期（2025Q1~2026Q2）；同理可向前回补。
- `relative_strength` 与 `momentum_60` 截面等价（见 §3.2），M3 聚类去重时会被合并。
- 未做行业/市值中性化后的 IC —— 现在是**原始因子**的 IC，价值因子受行业结构影响明显。
- 未做涨跌停/停牌过滤下的可交易性检验（数据已备好 `stk_limit`、`suspend_d`）。

**M3（因子筛选流水线）**：

1. 行业/市值中性化 → 重新评估 IC/ICIR；
2. 相关性聚类去重（35 → 20~25 个低相关因子）；
3. **月频调仓 + walk-forward** 样本外验证（解决 §3.2 的 t 值高估问题）；
4. 分层回测（多空组合年化/夏普/最大回撤/换手）—— 仓库已有 `quantile_backtest`。

**M4（条件组合回测 + 前端）**：条件 DSL 已就绪（`src/quant/condition_dsl.py`），
接上逐期截面筛选 + TopK + 成本模型即可出净值曲线。

---

## 七、M3 因子筛选流水线 + 前端接入（已完成）

### 7.1 流水线（`src/quant/screening.py`）

```
中性化（MAD去极值 → 市值/行业中性化 → z-score）
  → 训练集 IC/ICIR（只在前 70% 交易日上算）
  → 相关性聚类去重（训练集 pairwise-complete 秩相关，|ρ|≥阈值合并）
  → 门槛过滤（|IC|、|ICIR|）+ 目标数量截断
  → 样本外复核（后 30% 只用于评估，不参与任何选择）
  → 分层回测（训练集 / 样本外各跑一次，非重叠持有期）
```

与设计文档 §4.2 的三处**刻意不同**：

1. **ICIR 定义**：文档代码 `ic / factor.std()` 是因子截面标准差（错的），
   这里用 IC 时间序列的均值/标准差；
2. **强制 walk-forward**：文档没提样本外；这里筛选**只看训练集**；
3. **去重靠相关性聚类**：不是 Gram-Schmidt（GS 结果依赖顺序、残差不保证阈值、
   还会破坏因子的经济含义）。

### 7.2 真实数据实测（2026 年至今，171 交易日 × 5238 只）

```
训练集 120 个交易日（20260105~20260706） | 样本外 51 个（20260707~20260915）
相关性去重：35 个因子 → 25 个代表因子
训练集通过门槛（|IC|≥0.02, |ICIR|≥0.3）并去重后保留 7 个因子
训练集平均 |ICIR| 0.606 → 样本外 0.531（衰减 12%）

样例（样本内 → 样本外）：
  amihud         IC −0.0787  ICIR −0.953   → 样本外 ICIR −0.197
  net_margin     IC +0.0511  ICIR +0.776   → 样本外 ICIR −0.048
  roe            IC +0.0435  ICIR +0.702   → （未入选）
  total_mv       IC −0.0288  ICIR −0.624   → 样本外 ICIR −2.393
  ocf_to_profit  IC +0.0253  ICIR +0.599   → 样本外 ICIR +0.695（最稳）

分层回测（样本外，等权合成，5 组，20 日非重叠持有期）：
  多空年化 +115%  夏普 2.28  最大回撤 −3.4%
  分组年化单调：第1组 −34% → 第5组 +66%
```

**⚠️ 必须同时读的三条保留**：
① 样本外只有 **3 个非重叠持有期**，年化/夏普噪声极大（程序会在结果里自动打印这条警告）；
② 训练→样本外的 |ICIR| 衰减 12% 说明有衰减但未崩；`net_margin` 这类从 +0.78 掉到 −0.05
的因子正是"样本内好看、样本外失效"的典型，不能只看样本内；
③ 已做市值中性化，因此规模类因子的 IC 不具备"选股"含义。

### 7.3 前端接入（策略回测页 · 多因子模式）

`web/src/components/BacktestPanel.tsx` 顶部新增模式切换：
**「宏观择时」（原有）/「多因子（35 个因子）」**。多因子面板 `QuantFactorPanel.tsx` 提供：

- **因子库**：七大类 × 35 个因子，带中文标签/英文键/公式提示，方向自动取反的因子标 `↺`；
  支持按类别全选；
- **数据状态条**：本地缓存了哪些数据集、多少期、多少行（未就绪时给出下载命令）；
- **参数区**：起止日期、IC 前瞻天数、|IC|/|ICIR| 门槛、相关性阈值、训练集比例、
  目标因子数、是否市值中性化；
- **结果区**：ICIR 横向条形图（正绿负红）、样本外 IC 表、分层回测柱状图、
  去重与淘汰明细（谁被谁代替、原因）。

后端 API（`src/api/routes/quant.py`）：

| 接口 | 说明 |
|------|------|
| `GET /api/v1/quant/factors` | 35 个因子元信息（分类/公式/方向） |
| `GET /api/v1/quant/data-status` | 本地因子数据缓存体检 |
| `GET /api/v1/quant/ic` | 快速 IC 表（最近 N 天，不中性化，几秒返回） |
| `POST /api/v1/quant/screen` | 启动完整筛选（异步 job，返回 job_id） |
| `GET /api/v1/quant/screen/{job_id}` | 轮询筛选进度与结果 |

### 7.4 前端接入过程中又挖出并修掉的 3 个缺陷

| # | 缺陷 | 症状 | 根因与修复 |
|---|------|------|------------|
| 7 | **重计算把 web 服务算死** | 服务**无 traceback 消失**，日志停在最后一行；Windows 事件日志里没有 Application Error（→ 内存耗尽而非原生崩溃） | 35 因子 × 171 天 × 5238 只的中性化/IC/回测在服务进程里跑，峰值内存过高。修：新增 `src/quant/screen_runner.py`，筛选放进**子进程**执行（请求/结果走 JSON 文件），子进程崩了只影响这一次任务，服务照常 |
| 8 | **低覆盖因子让聚类静默失效** | 报告"35 个因子 → 35 个代表因子"，看起来像"没有相关因子" | 相关性矩阵用"当天所有因子都得有值"筛日期，`momentum_120`/`peg` 早期整列为空 → 所有训练日被跳过 → 矩阵全 NaN。修：改成**逐对可用（pairwise-complete）**。另外 `DataFrame.corr(method="spearman")` 需要 scipy（本项目未装）→ 自算"排名的 Pearson" |
| 9 | **因子筛选用全样本 IC** | 样本外 |ICIR| 反而比训练集高，得出"-100% 衰减"这种荒谬结论 | 门槛过滤/聚类代表/合成权重都用了全样本 IC = 用样本外信息挑因子。修：筛选全程只用训练集，样本外只用于评估 |
| 10 | **分层回测年化口径错** | 第 5 组"年化 46962%"、夏普 93598 | `quantile_backtest` 硬编码按日频 252 年化，而传入的是 **20 日前瞻收益**（还彼此重叠）。修：加 `periods_per_year` 参数 + 调用方做**非重叠抽样**（每 N 个交易日一期）。修复后同一份数据：多空年化 +115%、夏普 2.28 |

第 8、9 条特别值得记：两者都**不报错、数字看着也"正常"**（一个是"没有相关因子"，
一个是"样本外更强"），只有把结果和理论预期对照才会发现 —— 这也是为什么每步都要有
"构造已知答案 → 断言"的用例。

### 7.5 前端怎么用

1. 打开「策略回测」页 → 顶部切到 **多因子（35 个因子）**；
2. 顶部数据条确认本地数据就绪（需先跑过 `quant_sync.py download`）；
3. 默认不选因子 = 全部 35 个参与筛选；也可只勾选某几类做对比；
4. 参数保持默认（20 日前瞻、|IC|≥0.02、|ICIR|≥0.3、ρ≥0.7、训练集 70%）→ 点「开始筛选」；
5. 约 3~4 分钟出结果；看 ICIR 条形图、样本外表、分层柱状图与淘汰明细。

**注意**：筛选是 CPU/内存密集型任务，服务端已隔离到子进程；同一时间只跑一个任务体验最好。

---

## 附录：筛选性能优化实测（2026-09-16）

场景：171 个交易日 × 4260 只股票 × 10 个因子，walk-forward 0.7。

| 环节 | 优化前 | 优化后 | 关键动作 |
|------|-------:|-------:|----------|
| build_panels | 43.1s | 38.5s | 财务面板改走库（原来每次解压 106 个季度分区） |
| compute_factors | 60.4s | **2.0s** | 见下 |
| screen | 23.8s | **11.3s** | 秩相关向量化 |
| **端到端（含子进程）** | **115.4s** | **47.8s** | **2.4×** |

**结果逐位不变**（roe 的 IC 0.0386、多空 1.2081、夏普 1.7416 优化前后完全一致）。

### 三个真正的热点（profile 出来的，不是猜的）

1. **panels.fundamental_field 占 59.9s / 127s（47%）** —— 它每个交易日调一次
   PitPanel.as_of，每次对 33 万行财务记录做"过滤 + 排序 + 按 code 去重"。
   改用 as_of_panel 的一次 merge_asof 之后，compute_factors 从 60.4s 掉到 2.0s。
   **这个快路径其实早就写好了，只是一直没人用。**
2. **_rankdata 的 Python 双层 while 循环占 8.9s** —— 找并列区间的实现是逐元素
   解释器循环，而一次筛选要算 171 个截面 × 十几个因子 = 2500+ 次。
   换成 argsort + flatnonzero(diff) + add.reduceat 的纯向量化写法。
3. **财务面板每次从 CSV 读 106 个 gzip 分区** —— 库里 quant_fina_indicator
   一条查询就够。

### 优化过程中暴露出的一个既有缺陷（比优化本身更重要）

PitPanel.as_of_panel 的文档写着"与 as_of 两者结果必须一致（有专门用例对拍）"，
但**那个用例从来没有存在过**。切到快路径后才发现两条路径在多处不一致，
roe 列最大差异 **5942**：

- 同一 code 在同一 usable_date 上会有多条记录（不同报告期同日公告、修正公告）；
- merge_asof 排序默认是**非稳定**的，并列时取到哪一行未定义；
- 而 as_of 用 keep="last"，稳定排序下取"原始顺序里的最后一条"（= 最近的报告期）。

修法：两处排序都显式 kind="stable"，让快慢两条路径口径一致。

**教训**：文档里"有专门用例对拍"这句话本身是危险的 —— 它让人以为这条不变量被守着。
现在 tests/unit/test_quant_pit_parity.py 真的守了，而且**验证过它在缺陷存在时会失败**
（非稳定排序下 400/400 个 code 不一致；小数据集测不出来，必须给到几千行才会复现）。

另一个自己踩的坑：为了省时间把财务截面快照缓存在调用方 FactorPanels 上，
结果调用方替换 panels.fundamentals 之后返回**过期数据**且不报错。
缓存必须挂在不可变的 PitPanel 自身上。

### 还没做的（按预估收益排序）

- **按需加载面板字段**：build_panels 现在无条件加载全部 51 个字段
  （约 4600 万个数值），而一次筛选可能只用到其中几个。按请求裁剪列是下一块；
- **股票池过滤**：剔除流动性最差的 30%（业界常规做法）能同比例减少数据量，
  但会改变结果，需要作为显式选项而不是默认行为；
- **float32**：行物化成本与数据量成正比，降到单精度可再省一截；
- **build_panels 内部仍剩约 13s 的 sqlite fetchall**：这是千万级数值从 C 转
  Python 对象的固有成本，除非改用 Arrow/DuckDB 直读。