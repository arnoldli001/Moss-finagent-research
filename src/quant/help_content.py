"""操作说明书内容（由**代码本身**生成，避免文档与实现漂移）。

## 为什么说明书要由代码生成

手写的参数表一定会过期：改了一个默认值、加了一个算子，文档还停在旧版本，
而用户会照着旧文档写条件 —— 这类错误最难发现，因为文档看起来权威。

所以这里的原则是：**能从代码里读出来的，就不要手写**。

- 因子清单来自 `factor_library_v2.FACTORS`（键名、中文名、方向、公式）
- DSL 函数来自 `condition_dsl` 的函数集合常量
- 面板字段来自 `panels.PANEL_FIELDS` 与价格/财务字段
- 回测参数与默认值来自 `single_backtest.SingleBacktestConfig` / `CostConfig`
- 撮合规则、已知限制等无法从代码推导的部分才手写

`build_help(topic)` 返回结构化 JSON，前端直接渲染；`topic` 为
`factors`（多因子筛选页）或 `single`（单股票策略回测页）。
"""
from __future__ import annotations

from typing import Any

from src.quant.condition_dsl import (
    _CROSS_SECTION_FUNCS,
    _ELEMENT_FUNCS,
    _TS_FUNCS,
)

# 手写部分：这三块无法从代码推导（是设计意图与实证结论）
_SINGLE_NOTES: list[str] = [
    "**信号时点**：第 t 日收盘算出的信号，在第 t+1 日开盘成交。"
    "用当日收盘价成交＝用了收盘后才知道的信息，是最常见的未来函数。",
    "**T+1**：当日买入当日不可卖。",
    "**整手**：买入股数向下取整到 100 股 —— 10 万本金买不了一手茅台（约 13 万）"
    "是真实约束，回测会直接告诉你「资金不足」而不是给一条平的净值曲线。",
    "**涨停不买 / 跌停不卖 / 停牌不交易**：一字板挂单成交不了，"
    "回测里「成交了」是最典型的虚高来源。",
    "**止损与止盈同日触发**：一律按止损成交（无法确知盘中顺序，取不利一侧）。",
    "**价格空间**：开高低收是后复权价（比例正确），另有 `*_raw` 列是真实报价；"
    "写绝对价格阈值时请用 `close_raw`。",
    "**单股票样本量天然很小**：一只票 20 年也只有约 5000 个交易日，"
    "结论的统计置信度系统性低于全市场截面回测。",
]

_TS_EXAMPLES: dict[str, str] = {
    "MA": "close > MA(close, 20)",
    "MEAN_TS": "同 MA",
    "STD_TS": "STD_TS(close, 20) / close < 0.03",
    "SUM_TS": "SUM_TS(amount, 5) > 1e9",
    "MAX_TS": "close > REF(MAX_TS(close, 60), 1)",
    "MIN_TS": "close > MIN_TS(close, 20) * 1.05",
    "PCTL_TS": "PCTL_TS(close, 250) < 30",
    "ZSCORE_TS": "ZSCORE_TS(turnover_rate, 60) > 1",
    "REF": "close > REF(close, 5)",
    "DELTA": "DELTA(close, 250) > 0",
    "COUNT_TS": "COUNT_TS(close > MA(close,20), 10) >= 8",
}

_CROSS_EXAMPLES: dict[str, str] = {
    "rank": "RANK(pe_ttm) < 30",
    "pctl": "pe_ttm < PCTL(pe_ttm, 0.3)",
    "avg": "pe_ttm < AVG(pe_ttm)",
    "std": "pe_ttm < AVG(pe_ttm) - STD(pe_ttm)",
    "median": "pe_ttm < MEDIAN(pe_ttm)",
    "min": "close > MIN(close)",
    "max": "close < MAX(close)",
    "count": "COUNT() > 100",
}

_ELEMENT_EXAMPLES: dict[str, str] = {
    "abs": "ABS(DELTA(close, 20)) > 1",
    "log": "LOG(total_mv) > 25",
    "sqrt": "SQRT(amount) > 100",
    "sign": "SIGN(DELTA(close, 5)) > 0",
}


def _factor_catalog() -> list[dict[str, Any]]:
    """35 因子清单（从因子注册表读，不手写）。"""
    from src.quant.factor_library_v2 import factors_by_category

    catalog: list[dict[str, Any]] = []
    for category, specs in factors_by_category().items():
        catalog.append({
            "category": category,
            "count": len(specs),
            "factors": [{
                "key": spec.key,
                "label": getattr(spec, "label", spec.key),
                "direction": getattr(spec, "direction", 1.0),
                "formula": getattr(spec, "formula", ""),
                "note": getattr(spec, "note", ""),
            } for spec in specs],
        })
    return catalog


def _panel_fields() -> list[dict[str, Any]]:
    """条件里可以引用的面板字段（从 panels 的读取清单生成）。"""
    from src.quant.panels import BAK_FIELDS, BASIC_FIELDS, FLOW_FIELDS, WAREHOUSE_PRICE_FIELDS

    fields = [
        {"group": "价格（后复权，比例正确）", "columns": list(WAREHOUSE_PRICE_FIELDS),
         "note": "close/open/high/low 是后复权价；另有 close_raw / open_raw / "
                 "high_raw / low_raw 是真实报价，用于绝对价格阈值"},
        {"group": "估值与股本", "columns": list(BASIC_FIELDS),
         "note": "来自 daily_basic，逐日可查"},
        {"group": "资金流", "columns": list(FLOW_FIELDS),
         "note": "来自 moneyflow，2010 年起有数据"},
        {"group": "涨跌停价", "columns": ["up_limit", "down_limit"],
         "note": "来自 stk_limit；回测会自动换算到与成交价同一复权空间"},
        {"group": "备用行情", "columns": list(BAK_FIELDS),
         "note": "来自 bak_daily，2018 年起；有 16 个交易日 Tushare 源侧缺数"},
        {"group": "财务（PIT，按公告日对齐）",
         "columns": ["roe", "roa", "netprofit_margin", "grossprofit_margin",
                     "debt_to_assets", "ocf_to_profit", "eps", "bps",
                     "netprofit_yoy", "tr_yoy", "ocf_yoy", "roe_yoy"],
         "note": "公告日 +1 个自然日之后才可见（lag_days=1），绝不使用未公布财报"},
    ]
    return fields


def _dsl_reference(ts_mode: bool) -> dict[str, Any]:
    """DSL 函数手册：签名、含义、示例、以及"什么时候不能用"。"""
    if ts_mode:
        functions = [{
            "name": name.upper(),
            "signature": _ts_signature(name),
            "example": _TS_EXAMPLES.get(name.upper(), ""),
            "kind": "时序",
        } for name in sorted(_TS_FUNCS)]
        forbidden = {
            "functions": sorted(name.upper() for name in _CROSS_SECTION_FUNCS),
            "why": "单股票模式下这些算子的求值对象是**整条序列**："
                   "`RANK(PE)` 会拿今天和未来所有交易日一起排名，是未来函数 —— "
                   "算出来还很像样、不报错，但回测收益凭空变好且无法复现。"
                   "解析阶段就会拒绝，请改用对应的时序版本。",
        }
    else:
        functions = [{
            "name": name.upper(),
            "signature": _cross_signature(name),
            "example": _CROSS_EXAMPLES.get(name, ""),
            "kind": "截面",
        } for name in sorted(_CROSS_SECTION_FUNCS)]
        forbidden = {
            "functions": sorted(name.upper() for name in _TS_FUNCS),
            "why": "截面模式下每一行是一只股票、没有「历史窗口」可言，"
                   "因此时序函数不可用（要用请到「单股票策略回测」页）。",
        }
    functions.extend({
        "name": name.upper(),
        "signature": f"{name.upper()}(x)",
        "example": _ELEMENT_EXAMPLES.get(name, ""),
        "kind": "逐元素",
    } for name in sorted(_ELEMENT_FUNCS))
    return {
        "functions": functions,
        "forbidden": forbidden,
        "operators": {
            "比较": ["=", "!=", ">", ">=", "<", "<=", "BETWEEN a AND b",
                     "IN ('a','b')", "NOT IN (...)"],
            "逻辑": ["AND", "OR", "NOT", "括号 () 可改变优先级"],
            "算术": ["+", "-", "*", "/"],
            "优先级": "NOT > AND > OR；比较和算术按常规优先级",
        },
        "pit": "数据缺失按三值逻辑处理：未知 → **不入选**（不是当成 False 也不是 True）。"
               "`PE != 30` 在 pandas 里对 NaN 会返回 True，这里不会 —— "
               "否则「未知」会被当成「满足条件」混进结果。",
    }


def _ts_signature(name: str) -> str:
    if name in ("ma", "mean_ts", "std_ts", "sum_ts", "max_ts", "min_ts"):
        return f"{name.upper()}(序列, 窗口天数)"
    if name == "count_ts":
        return "COUNT_TS(条件, 窗口天数)"
    if name in ("ref", "delta", "pctl_ts", "zscore_ts"):
        return f"{name.upper()}(序列, 窗口天数)"
    return f"{name.upper()}(...)"


def _cross_signature(name: str) -> str:
    if name == "count":
        return "COUNT()"
    if name == "pctl":
        return "PCTL(序列, 分位0~1)"
    return f"{name.upper()}(序列)"


def _single_params() -> list[dict[str, Any]]:
    """回测参数表（默认值从 dataclass 读，不手写）。"""
    from src.quant.single_backtest import CostConfig, SingleBacktestConfig

    def row(field: Any, meaning: str) -> dict[str, Any]:
        default = field.default if field.default is not None else (
            field.default_factory() if field.default_factory is not None  # type: ignore[misc]
            else None)
        return {"name": field.name, "default": default,
                "type": str(field.type), "meaning": meaning}

    meanings = {
        "code": "标的代码（6 位）",
        "entry": "入场条件（DSL，时序模式）",
        "exit": "出场条件；留空则只靠止损/止盈/最长持有离场",
        "initial_cash": "初始资金（元）",
        "position_pct": "每次买入使用的资金比例",
        "stop_loss_pct": "止损幅度（0 = 不启用）；跳空低于止损价时按开盘价成交",
        "take_profit_pct": "止盈幅度（0 = 不启用）",
        "max_hold_days": "最长持有交易日（0 = 不限制）",
        "min_hold_days": "最短持有交易日",
        "t_plus_1": "是否遵守 T+1",
        "respect_price_limits": "涨停不买 / 跌停不卖",
        "respect_suspension": "停牌不交易",
        "train_ratio": "训练集比例（用于样本内外拆分）",
        "index_code": "指数基准代码（默认沪深300）",
        "recent_days": "「近一年」分段长度（交易日）",
        "costs": "交易成本（见下表）",
    }
    rows = [row(field, meanings.get(field.name, ""))
            for field in SingleBacktestConfig.__dataclass_fields__.values()]
    cost_meaning = {
        "commission_rate": "佣金费率（双边）",
        "min_commission": "单笔最低佣金（元）",
        "stamp_tax_rate": "印花税（**仅卖出**）",
        "transfer_fee_rate": "过户费（双边）",
        "slippage_bps": "滑点（基点，1bp = 0.01%）",
    }
    rows.append({"name": "costs", "default": {
        field.name: (field.default if field.default is not None else None)
        for field in CostConfig.__dataclass_fields__.values()},
        "type": "CostConfig",
        "meaning": "；".join(f"{key}: {value}"
                             for key, value in cost_meaning.items())})
    return rows


def build_help(topic: str) -> dict[str, Any]:
    """返回说明书内容。"""
    if topic == "single":
        return {
            "topic": "single",
            "title": "单股票多因子条件策略回测 · 使用说明书",
            "sections": [
                {"heading": "一分钟上手",
                 "items": [
                     "① 填标的代码（如 600519）与初始资金；",
                     "② 在「入场条件」写买入规则，例如 "
                     "`close > MA(close, 20) AND momentum_20 > 0`"
                     "（条件里能用的因子名来自「多因子（35 个因子）」页的因子库 ——"
                     "先用那一页筛掉无效与重复的因子，再来这里写规则）；",
                     "③ 「出场条件」可留空，改用止损/止盈/最长持有；",
                     "④ 点「开始回测」——信号在第 t 日收盘产生、第 t+1 日开盘成交；",
                     "⑤ 看结果里的**样本外**与**近一年**两段，别只看全样本；",
                     "⑥ 满意就点「一键保存到策略库」（同一套参数重复保存只更新）。",
                 ]},
                {"heading": "撮合口径（每一条都影响结论，务必知道）",
                 "items": _SINGLE_NOTES},
                {"heading": "条件 DSL：时序函数（本页可用）",
                 "items": [f"`{item['signature']}` —— 例：`{item['example']}`"
                           for item in _dsl_reference(True)["functions"]
                           if item["kind"] == "时序"]},
                {"heading": "条件 DSL：逐元素函数（本页可用）",
                 "items": [f"`{item['signature']}` —— 例：`{item['example']}`"
                           for item in _dsl_reference(True)["functions"]
                           if item["kind"] == "逐元素"]},
                {"heading": "条件 DSL：运算符",
                 "items": [f"{key}：{'、'.join(value)}"
                           for key, value in
                           _dsl_reference(True)["operators"].items()]},
            ],
            "dsl": _dsl_reference(True),
            "panel_fields": _panel_fields(),
            "factors": _factor_catalog(),
            "params": _single_params(),
            "pit_rule": _dsl_reference(True)["pit"],
            "limitations": [
                "样本量天然很小：单只票 20 年约 5000 个交易日，"
                "近一年只有约 240 个交易日；交易笔数 <10 时胜率/盈亏比基本是噪声。",
                "不做参数寻优（没有网格搜索）—— 避免鼓励「调参调到好看」。",
                "不含分红现金流的细节：用后复权价等价于「分红再投资」。",
                "止损用日线最低价判断，无法区分盘中是否真的成交，属偏乐观近似。",
                "「跑赢买入持有」在一只长期上涨的存活股上极难做到 —— "
                "回测结果页因此同时给出**指数基准**与**风险调整维度**"
                "（回撤、Calmar），而不是只比最终金额。",
            ],
        }
    if topic == "factors":
        return {
            "topic": "factors",
            "title": "多因子筛选 · 使用说明书",
            "sections": [
                {"heading": "这个页面做什么（先读这一条）",
                 "items": [
                     "回答**截面**问题：把全市场 5000 多只票在每个交易日按因子排序分组，"
                     "哪一组更好、哪些因子有效。",
                     "它**不回答**「我就交易这一只票该怎么买」—— 那要用"
                     "「单股票策略回测」页。两者视角不同，不可互相替代。",
                     "**点「开始筛选」不是跑一次回测看赚多少钱**，而是给 35 个候选因子"
                     "做体检：剔掉无效的、合并重复的、并检查样本外还剩多少。"
                     "它的产出是「下一环节的输入」（因子清单 + 权重），不是一个收益承诺。",
                     "为什么不能只勾自己看好的几个：「全选让数据替我筛」才是这个页面的用法。"
                     "只勾 3 个已经先入为主，筛选页就退化成给自己结论背书的工具；"
                     "勾选真正的用途是做**分组对比**（例如只勾价值+质量，看是否比动量类更稳）。",
                     "为什么因子要在**全市场**上验证，而不是在一只票上："
                     "单只票近一年只有约 240 个交易日，单票 IC 序列还高度自相关，"
                     "用它判断「这个因子有没有用」等于用一个样本估计总体。"
                     "因子有效性靠的是横截面宽度（5000 只票同日排序），不是时序长度。",
                     "两页怎么串联：本页筛出「哪几个因子值得用、怎么加权」→ 把因子名写进"
                     "「单股票策略回测」的入场条件（如 `momentum_20 > 0`）→ 在具体标的上"
                     "验证买卖规则（含 T+1、涨跌停、停牌、交易成本）。",
                 ]},
                {"heading": "参数分两类：哪些能改、哪些不能改",
                 "items": [
                     "**样本区间**（开始/结束）：用哪一段历史做检验。区间越短，样本外结论"
                     "越不可信 —— 本地数据已回补至 2006 年，不必只填今年。",
                     "**检验口径**（IC 前瞻、|IC|/|ICIR| 门槛、相关性阈值 ρ、训练集比例、"
                     "目标因子数、市值中性化、剔除 ST）：只决定「多严格算有效因子」，"
                     "**不改变因子怎么算**。",
                     "**剔除 ST**（默认关闭）：按**当时的历史名称**判定，不是今天的名字 ——"
                     "数据来自 Tushare `namechange`（补数据："
                     "`python scripts/quant_sync.py download --namechange-only "
                     "--start 2006-01-01 --end <今天>`）。默认关闭是因为它会改变截面构成；"
                     "打开后结果里会写明剔除了多少个「股票日」。",
                     "**股票池过滤**（默认关闭）：按**过去 20 个交易日**的平均成交额排序，"
                     "每日剔除最差的 30%（业界常规做法）。它同样改变截面构成（僵尸股不再"
                     "参与 IC）；顺带让面板的列少一截，长区间才跑得动。排序只用过去数据，"
                     "窗口头几天算不出均值时整天放行（不会把截面剔空）。",
                     "**因子自身的参数是不可调的**：窗口写死在因子定义里，"
                     "`momentum_20` / `momentum_60` / `momentum_120` 是三个不同的因子，"
                     "不是同一个因子的三个参数。本项目**不做参数寻优（没有网格搜索）** ——"
                     "35 个因子一起调参会变成标准的过拟合机器。",
                 ]},
                {"heading": "一分钟上手",
                 "items": [
                     "① 确认顶部「本地数据」有分区与行数（若显示 0，见下方排查）；",
                     "② 设置起止日期（样本外质量取决于区间长度，越短越不可信）；",
                     "③ 勾选参与筛选的因子（默认 35 个全选，可只留一类）；",
                     "④ 点「开始筛选」，等待子进程返回；",
                     "⑤ 先读**样本外**那张表，再读相关性聚类与分层回测；",
                     "⑥ 注意结果里的样本量警告 —— 非重叠持有期少于 8 期时年化没有意义。",
                 ]},
                {"heading": "结果怎么读",
                 "items": [
                     "**IC / ICIR**：IC 是因子值与未来收益的截面相关性；"
                     "ICIR = IC 均值 / IC 标准差，衡量稳定性。|ICIR| < 0.3 通常不值得用。",
                     "**训练集 / 样本外**：筛选只在前 70% 交易日上进行，"
                     "后 30% 只用于检验 —— 若用全样本挑因子，样本外指标会明显虚高。",
                     "**相关性聚类**：|ρ| 超过阈值的因子归为一簇，每簇只留 |ICIR| 最高者。"
                     "这样去重比 Gram-Schmidt 正交化更稳，也不会改掉因子的经济含义。",
                     "**分层回测**：按合成因子分 N 组，看多空收益与单调性；"
                     "按持有期**非重叠抽样**，年化系数用 252/持有期。"
                     "⚠️ 它是**因子有效性检验，不是可交易策略**：A 股空头腿做不了，"
                     "组合等权、未扣交易成本，所以「多空年化」不等于能拿到的收益；"
                     "样本外非重叠持有期少于 8 期时，这个年化数字基本是噪声。",
                     "**市值中性化**：规模类因子（total_mv/log_mv/free_float_mv）"
                     "与中性化变量共线，它们的 IC 仅供参考，不宜据此选股。",
                     "**选出来的因子名可以直接用**：把 `selected` 里的因子写进"
                     "「单股票策略回测」的入场条件（如 `momentum_20 > 0 AND roe > 0`）"
                     "即可在该票上做时序验证。",
                 ]},
                {"heading": "已知边界",
                 "items": [
                     "不含北交所；行业分类是申万/东财混合口径。",
                     "停牌与涨跌停在截面 IC 里按缺失处理，不做特殊加权。",
                     "factor_analyzer 的分层回测用等权组合，未扣交易成本 —— "
                     "换手率高的因子实际收益会明显低于回测值。",
                     "**面板按需装配**：一次筛选只装这次真正用到的字段（35 个因子实际"
                     "只需要十几个，而不是把 40 多个字段全装进内存）。结果是"
                     "「能筛的区间变长了、跑得更快了」，IC 与分层回测的口径没有任何变化；"
                     "万一漏装字段会**直接报错**，不会悄悄算出一列 NaN。",
                     "**区间越长内存越高，有硬约束**：面板按「交易日 × 全市场 × 实际字段数」"
                     "装进内存。**服务端有面板预算护栏**（默认 8 GB）会提前几秒拒绝并给出"
                     "替代方案（缩短区间 / 少选因子 / 打开股票池过滤 / 用 "
                     "`MOSS_SCREEN_MAX_PANEL_MB` 放宽），而不是跑到一半被系统杀掉。",
                     "本地数据已回补至 2006 年（5000+ 个交易日），但**默认区间是 "
                     "2024-01-01 起**：样本外期数随区间长度增长，只筛最后一两年时"
                     "年化/夏普只能当方向性参考；想看跨牛熊的结论就把开始日期往前推"
                     "（2015 年起约半小时）。",
                 ]},
            ],
            "dsl": _dsl_reference(False),
            "factors": _factor_catalog(),
            "troubleshooting": [
                {"symptom": "本地数据 0 万行 / daily 0 期",
                 "cause": "web 服务进程是**旧版本**（修复前启动的），"
                          "它读不到双格式缓存，响应里也没有仓库字段。",
                 "fix": "重启服务：`C:\\veighna_studio\\python.exe manage.py start --replace`。"
                        "数据本身没问题 —— 用 `python scripts/quant_warehouse.py status` "
                        "与 `python -m src.quant.dataset_store` 可直接核对。"},
                {"symptom": "缓存里只有 N 个交易日，至少需要 40 天",
                 "cause": "所选区间内没有下载过数据。",
                 "fix": "先跑 `python scripts/quant_sync.py download --start 2006-01-01 "
                        "--end <今天>`，再 `python scripts/quant_warehouse.py ingest`。"},
                {"symptom": "仓库未启用（回测走 CSV 分区）",
                 "cause": "服务进程是旧版本（新版本会显示仓库方言与行数），"
                          "或数据库不可用。",
                 "fix": "重启服务；数据库连接见 `python scripts/quant_warehouse.py status`。"},
            ],
        }
    raise KeyError(f"未知说明书主题 {topic!r}；可用：single / factors")


__all__ = ["build_help"]
