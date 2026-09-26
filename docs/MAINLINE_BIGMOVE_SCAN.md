# 候选池的「大行情」因子扫描

> 由 `scripts/pool_bigmove_scan.py` 生成，评分表 `mainline_score`。
> 标签：未来 20 日收盘收益 > 10%（`big`），以及**相对当日全市场中位数**的超额 > 0（`rel_big`，纯横截面口径）。
> 特征全部取**当日横截面分位**，消除跨日尺度漂移；三个窗口同向且 bootstrap 5% 分位 > 0.5 才算规律。

- 【旧 20231009~20240806】候选行 12169，交易日 204，特征 145 个
- 【中 20240901~20250930】候选行 16689，交易日 263，特征 145 个
- 【新 20251001~20260918】候选行 14976，交易日 234，特征 145 个

## 一、单因子：`rel_big` AUC（按三窗口最差排序）

| 因子 | 旧 AUC | 中 AUC | 新 AUC | 最差 | 行数 | 基准率 |
|---|---:|---:|---:|---:|---:|---:|
| `six_dim.prosperity.raw.revenue_yoy` | 0.5297 | 0.5478 | 0.5227 | **0.5227** | 16689 | 56.6% |
| `accumulation.leverage.raw.form_b` | 0.5379 | 0.5221 | 0.5150 | **0.5150** | 14934 | 56.7% |
| `accumulation.volume_price.raw.form_b` | 0.5311 | 0.5147 | 0.5150 | **0.5147** | 16589 | 56.7% |
| `accumulation.northbound.raw.form` | 0.5178 | 0.5132 | 0.5102 | **0.5102** | 16589 | 55.7% |
| `accumulation.northbound.raw.strength` | 0.5178 | 0.5132 | 0.5102 | **0.5102** | 16589 | 55.7% |
| `six_dim_coverage` | 0.5410 | 0.5100 | 0.5233 | **0.5100** | 16689 | 56.6% |
| `accumulation.etf.raw.form` | 0.5070 | 0.5319 | 0.5167 | **0.5070** | 4662 | 60.2% |
| `accumulation.etf.raw.strength` | 0.5070 | 0.5319 | 0.5167 | **0.5070** | 4662 | 60.2% |
| `accumulation.volume_price` | 0.5172 | 0.5132 | 0.5063 | **0.5063** | 16589 | 56.7% |
| `accumulation.volume_price.raw.form` | 0.5172 | 0.5132 | 0.5063 | **0.5063** | 16589 | 56.7% |
| `accumulation.volume_price.raw.strength` | 0.5172 | 0.5132 | 0.5063 | **0.5063** | 16589 | 56.7% |
| `accumulation.leverage.raw.persistence` | 0.5089 | 0.5041 | 0.5191 | **0.5041** | 14976 | 56.6% |
| `accumulation.northbound.raw.persistence` | 0.5089 | 0.5041 | 0.5279 | **0.5041** | 13440 | 55.6% |
| `accumulation.etf` | 0.5039 | 0.5283 | 0.5105 | **0.5039** | 4668 | 60.1% |
| `accumulation.volume_price.raw.persistence` | 0.5083 | 0.5033 | 0.5191 | **0.5033** | 14934 | 56.7% |
| `accumulation.leverage.raw.form` | 0.5183 | 0.5030 | 0.5063 | **0.5030** | 14934 | 56.7% |
| `accumulation.leverage.raw.strength` | 0.5183 | 0.5030 | 0.5063 | **0.5030** | 14934 | 56.7% |
| `resonance` | 0.5027 | 0.5079 | 0.5104 | **0.5027** | 16689 | 56.6% |
| `bonus_potential` | 0.5026 | 0.5046 | 0.5108 | **0.5026** | 16689 | 56.6% |
| `accumulation.northbound.raw.form_b` | 0.5320 | 0.5147 | 0.5013 | **0.5013** | 16589 | 55.7% |
| `promoted` | 0.5174 | 0.5100 | 0.5000 | **0.5000** | 16689 | 56.6% |
| `six_dim.macro.raw.common` | 0.5174 | 0.5100 | 0.5000 | **0.5000** | 16689 | 56.6% |
| `accumulation.leverage.raw.low` | 0.4990 | 0.5413 | 0.5026 | **0.4990** | 14976 | 56.6% |
| `accumulation.northbound.raw.low` | 0.4990 | 0.5413 | 0.4986 | **0.4986** | 13440 | 55.6% |
| `accumulation.etf.raw.trigger` | 0.4958 | 0.5008 | 0.5001 | **0.4958** | 4668 | 60.1% |
| `accumulation.volume_price.raw.change_rank` | 0.5091 | 0.5024 | 0.4957 | **0.4957** | 14934 | 56.7% |
| `accumulation.volume_price.raw.change` | 0.5091 | 0.5024 | 0.4957 | **0.4957** | 14934 | 56.7% |
| `accumulation.leverage.raw.change_rank` | 0.5092 | 0.5036 | 0.4957 | **0.4957** | 14976 | 56.6% |
| `accumulation.leverage.raw.change` | 0.5092 | 0.5036 | 0.4957 | **0.4957** | 14976 | 56.6% |
| `accumulation.northbound.raw.change_rank` | 0.5092 | 0.5036 | 0.4955 | **0.4955** | 13440 | 55.6% |
| `accumulation.northbound.raw.change` | 0.5092 | 0.5036 | 0.4955 | **0.4955** | 13440 | 55.6% |
| `accumulation.etf.raw.etf_share_rank` | 0.5014 | 0.5009 | 0.4954 | **0.4954** | 4668 | 60.1% |
| `accumulation.etf.raw.etf_share` | 0.5014 | 0.5009 | 0.4953 | **0.4953** | 4668 | 60.1% |
| `accumulation.volume_price.raw.etf_share_rank` | 0.5011 | 0.5010 | 0.4953 | **0.4953** | 4662 | 60.2% |
| `accumulation.volume_price.raw.etf_share` | 0.5011 | 0.5010 | 0.4952 | **0.4952** | 4662 | 60.2% |
| `accumulation.northbound.raw.etf_streak` | 0.5105 | 0.5008 | 0.4952 | **0.4952** | 4154 | 58.9% |
| `accumulation.northbound.raw.etf_share_rank` | 0.4987 | 0.5009 | 0.4938 | **0.4938** | 4154 | 58.9% |
| `accumulation.northbound.raw.etf_share` | 0.4987 | 0.5009 | 0.4937 | **0.4937** | 4154 | 58.9% |
| `accumulation.etf.raw.etf_streak` | 0.5073 | 0.5008 | 0.4936 | **0.4936** | 4668 | 60.1% |
| `accumulation.leverage.raw.etf_streak` | 0.5114 | 0.5016 | 0.4936 | **0.4936** | 4668 | 60.1% |

## 二、基准对照：现有打分的两把尺子

| 因子 | 旧 AUC | 中 AUC | 新 AUC | 最差 |
|---|---:|---:|---:|---:|
| `total` | 0.5305 | 0.4878 | 0.4921 | **0.4878** |
| `base_total` | 0.5386 | 0.4806 | 0.4865 | **0.4806** |
| `six_dim_score` | 0.5322 | 0.4794 | 0.5031 | **0.4794** |
| `accumulation_score` | 0.5107 | 0.5042 | 0.4789 | **0.4789** |
| `rank_neg` | 0.5322 | 0.4795 | 0.5031 | **0.4795** |

## 三、入选因子与按日 bootstrap

门槛：三窗口 `rel_big` AUC 点估计均 ≥ 0.52，且 bootstrap 5% 分位均 > 0.5。

| 因子 | 旧 AUC [5%,95%] | 中 AUC [5%,95%] | 新 AUC [5%,95%] | 判定 |
|---|---:|---:|---:|---:|
| `six_dim.prosperity.raw.revenue_yoy` | 0.5297 [0.5153,0.5442] | 0.5478 [0.5310,0.5641] | 0.5227 [0.5103,0.5374] | ✅ 入选 |

## 四、贪心组合（等权分位，最大化三窗口最差 AUC）

| 步 | 加入因子 | 旧 | 中 | 新 | 最差 |
|---:|---|---:|---:|---:|---:|
| 1 | `six_dim.prosperity.raw.revenue_yoy` | 0.5297 | 0.5478 | 0.5227 | **0.5227** |

最终组合（1 个因子，等权）：`six_dim.prosperity.raw.revenue_yoy`
三窗口最差 `rel_big` AUC = **0.5227**

## 五、组合分位的**头部命中率**（这才是能用的东西）

在候选池内按组合分位取前 20% / 后 20%，看未来 20 日真涨超 10% 的比例。

| 窗口 | 候选池基准 | 前 20% | 后 20% | 前 20% 相对基准 |
|---|---:|---:|---:|---:|
| 旧 | 6.7% | **7.5%** | 6.2% | +12% |
| 中 | 22.1% | **26.8%** | 14.7% | +21% |
| 新 | 15.7% | **20.8%** | 7.8% | +33% |
