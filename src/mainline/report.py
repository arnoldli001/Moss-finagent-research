"""主线挖掘：监控胜率报告（需求六的六章 Markdown）。

## 为什么报告由代码生成而不是 LLM 写

这份报告的每一个数字都要能被复算。让 LLM "根据数据写一段总结"会引入一个
不可复算的环节：同一个回测跑两次，结论的措辞会变，而读者无法判断哪次是真的。
因此这里**只用确定性代码**把 `BacktestReport` 渲染成 Markdown；
将来若要加"机器解读"，应该是**在报告之后**附一段独立标注来源的评论，
而不是让它参与生成正文。

## 六章（与需求六一一对应）

    第一章 总体绩效概览     区间 / 告警次数 / 综合胜率 / IC / 多空年化 / 多头超额
    第二章 分场景绩效       四个历史案例的触发时间、提前天数、最大涨幅
    第三章 分维度绩效       第一层六维 + 第二层三维各自的 IC / ICIR / 胜率
    第四章 告警信号流水     时间倒序，含告警后 5/10/20/60 日涨幅与最大涨幅
    第五章 假阳性分析       触发但未兑现的板块
    第六章 因子相关性验证   去重效果检验（跨层相关性矩阵与高相关因子对）

## 三个刻意的写作纪律

1. **"没有数据"必须写成"没有数据"**，不是"—"也不是 0。
   报告是给人做决策看的，`None` 被渲染成 `0.00` 会让人以为"这个指标很差"，
   而事实是"这个指标算不出来"。
2. **目标对照只标注达标与否，不改数字。** 把没达标的 IC 四舍五入成达标
   是自欺欺人；报告的价值恰恰在于让人看到差距。
3. **缺口单列一节。** 数据缺口（板块指数缺失、北向只到 2024-08、
   外盘行情未接入）写在报告末尾，而不是散落在各章 —— 读者需要一眼看到
   "这份报告在什么条件下成立"。
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from src.mainline.backtest import FACTOR_LABELS
from src.mainline.config import MainlineConfig
from src.mainline.models import BacktestReport, SignalLevel

#: 等级显示（报告里用文字而不是 emoji，便于复制到文档/邮件）
LEVEL_TEXT = {"strong": "强", "medium": "中", "weak": "弱", "none": "—"}


def render(report: BacktestReport, config: MainlineConfig | None = None) -> str:
    """把回测报告渲染成 Markdown（六章）。"""
    lines: list[str] = []
    lines.append("# 主线挖掘 · 监控胜率报告")
    lines.append("")
    lines.append(f"- 回测区间：`{report.range_start}` ~ `{report.range_end}`")
    lines.append(f"- 运行 ID：`{report.run_id}`")
    lines.append(f"- 开始 / 结束：{report.started_at} / {report.finished_at}"
                 f"（耗时 {report.seconds:.1f} 秒）")
    if report.config_note:
        lines.append(f"- 模型口径：{report.config_note}")
    if report.error:
        lines.append(f"- ⚠️ **本次回测未完整跑完**：{report.error}")
    lines.append("")
    lines.extend(_chapter_overview(report))
    lines.extend(_chapter_scenes(report))
    lines.extend(_chapter_dims(report))
    lines.extend(_chapter_signals(report))
    lines.extend(_chapter_false_positives(report))
    lines.extend(_chapter_correlation(report))
    lines.extend(_chapter_gaps(report))
    lines.append("")
    lines.append("---")
    lines.append("")
    lines.append(report.disclaimer or "本报告为量化统计结果，不构成投资建议。")
    return "\n".join(lines)


# ==================================================================
# 第一章
# ==================================================================


def _chapter_overview(report: BacktestReport) -> list[str]:
    metrics = report.metrics
    out = ["## 第一章 · 总体绩效概览", ""]
    signals = report.signals
    counts = {level: sum(1 for item in signals if item.level is level)
              for level in (SignalLevel.STRONG, SignalLevel.MEDIUM,
                            SignalLevel.WEAK)}
    out.append("| 项目 | 数值 | 目标 | 达标 |")
    out.append("|---|---:|---:|:---:|")
    out.append(f"| 回测区间 | {report.range_start} ~ {report.range_end} | — | — |")
    out.append(f"| 告警总次数 | {len(signals)} | — | — |")
    out.append(f"| 　🔴 强信号 | {counts[SignalLevel.STRONG]} | — | — |")
    out.append(f"| 　🟡 中信号 | {counts[SignalLevel.MEDIUM]} | — | — |")
    out.append(f"| 　🟢 弱信号 | {counts[SignalLevel.WEAK]} | — | — |")
    out.append(_row("IC 均值", metrics.ic_mean, metrics.targets.get("ic_mean"),
                    pct=True))
    out.append(_row("ICIR", metrics.icir, metrics.targets.get("icir")))
    out.append(_row("IC 胜率", metrics.ic_win_rate,
                    metrics.targets.get("ic_win_rate"), pct=True))
    out.append(_row("多空年化收益", metrics.long_short_annual,
                    metrics.targets.get("long_short_annual"), pct=True))
    out.append(_row("多头超额", metrics.long_excess,
                    metrics.targets.get("long_excess"), pct=True))
    out.append(_row("信号兑现率（20 日涨幅 > 5%）", metrics.signal_hit_rate,
                    metrics.targets.get("signal_hit_rate"), pct=True))
    out.append(_row("假阳性率（20 日未上涨）", metrics.false_positive_rate,
                    metrics.targets.get("false_positive_rate"), pct=True))
    out.append(_row("平均最大涨幅（60 日）", metrics.avg_max_gain,
                    metrics.targets.get("avg_max_gain"), pct=True))
    out.append("")
    out.append(f"- IC 有效样本：{metrics.ic_samples} 个交易日")
    out.append(f"- 多头年化 {_fmt(metrics.long_annual, pct=True)}；"
               f"基准年化 {_fmt(metrics.benchmark_annual, pct=True)}；"
               f"夏普 {_fmt(metrics.sharpe)}；"
               f"最大回撤 {_fmt(metrics.max_drawdown, pct=True)}")
    if metrics.by_holding:
        out.append("")
        out.append("**分持有期**")
        out.append("")
        out.append("| 持有期(交易日) | 样本 | 多头年化 | 基准年化 | 多头超额 | 胜率 |")
        out.append("|---:|---:|---:|---:|---:|---:|")
        for key in sorted(metrics.by_holding, key=lambda item: int(item)):
            row = metrics.by_holding[key]
            out.append(f"| {key} | {row.get('samples', '—')} | "
                       f"{_fmt(row.get('long_annual'), pct=True)} | "
                       f"{_fmt(row.get('benchmark_annual'), pct=True)} | "
                       f"{_fmt(row.get('long_excess'), pct=True)} | "
                       f"{_fmt(row.get('win_rate'), pct=True)} |")
    out.append("")
    return out


def _row(label: str, value: Any, target: Any = None, *, pct: bool = False
         ) -> str:
    mark = "—"
    target_text = "—"
    if isinstance(target, dict):
        target_value = target.get("target")
        passed = target.get("passed")
        target_text = _fmt(target_value, pct=pct)
        mark = "✅" if passed else ("⚠️" if passed is False else "—")
    return (f"| {label} | {_fmt(value, pct=pct)} | {target_text} | {mark} |")


# ==================================================================
# 第二章
# ==================================================================


def _chapter_scenes(report: BacktestReport) -> list[str]:
    out = ["## 第二章 · 分场景绩效", ""]
    if not report.scenes:
        out.append("本次回测没有配置分场景验证案例"
                   "（见 `configs/mainline.yaml` 的 `backtest.scenes`）。")
        out.append("")
        return out
    out.append("| 案例 | 启动日 | 检验窗口 | 是否触发 | 首次告警日 | 板块 | "
               "提前(自然日) | 要求 | 最大涨幅 | 60日涨幅 | 结论 |")
    out.append("|---|---|---|---|---|---|---:|---:|---:|---:|:---:|")
    for case in report.scenes:
        window = "~".join(item for item in case.check_window if item) or "—"
        out.append(
            f"| {case.label} | {case.start_date} | {window} | "
            f"{'是' if case.triggered else '否'} | {case.first_alert_date or '—'} | "
            f"{case.first_alert_board or '—'} | "
            f"{case.lead_days if case.lead_days is not None else '—'} | "
            f"≥{case.min_lead_days} | {_fmt(case.max_gain_pct)} | "
            f"{_fmt(case.ret_60d)} | {'✅' if case.passed else '⚠️'} |")
    out.append("")
    out.append("> 提前天数按**自然日**计算（案例启动日可能落在非交易日）；"
               "最大涨幅与 60 日涨幅的单位是 **%**。")
    out.append("")
    for case in report.scenes:
        if case.note:
            out.append(f"- **{case.label}**：{case.note}")
    out.append("")
    return out


# ==================================================================
# 第三章
# ==================================================================


def _chapter_dims(report: BacktestReport) -> list[str]:
    out = ["## 第三章 · 分维度绩效", ""]
    by_dim = report.metrics.by_dim or {}
    if not by_dim:
        out.append("没有可用的分维度统计（多数维度缺数据）。")
        out.append("")
        return out
    out.append("| 维度 | 样本 | IC 均值 | ICIR | IC 胜率 | 备注 |")
    out.append("|---|---:|---:|---:|---:|---|")
    for key in sorted(by_dim, key=lambda item: -(by_dim[item].get("ic_mean") or -9)):
        row = by_dim[key]
        out.append(f"| {key} | {row.get('samples', '—')} | "
                   f"{_fmt(row.get('ic_mean'))} | {_fmt(row.get('icir'))} | "
                   f"{_fmt(row.get('ic_win_rate'), pct=True)} | "
                   f"{row.get('note', '')} |")
    out.append("")
    out.append("> 分维度 IC 用于识别「当前市场环境下哪些维度最有效」。"
               "样本偏少的维度不要据此下结论。")
    out.append("")
    if report.folds:
        out.append("**Walk-Forward folds（前 20 个）**")
        out.append("")
        out.append("| # | 训练区间 | purge 至 | 验证区间 | 板块数 | 告警 |")
        out.append("|---:|---|---|---|---:|---:|")
        for fold in report.folds[:20]:
            out.append(f"| {fold.index} | {fold.train_start}~{fold.train_end} | "
                       f"{fold.purge_end} | {fold.validate_start}~"
                       f"{fold.validate_end} | {fold.boards} | {fold.alerts} |")
        out.append("")
    return out


# ==================================================================
# 第四章
# ==================================================================


def _chapter_signals(report: BacktestReport) -> list[str]:
    out = ["## 第四章 · 告警信号流水", ""]
    if not report.signals:
        out.append("本次回测区间内没有触发任何告警。")
        out.append("")
        return out
    out.append(f"共 {len(report.signals)} 条（下表最多列 300 条，按时间倒序）。")
    out.append("")
    out.append("| 告警日 | 板块 | 预警分 | 等级 | 触发维度 | 当日涨跌 | "
               "5日 | 10日 | 20日 | 60日 | 最大涨幅 | 出现日 | 确认 |")
    out.append("|---|---|---:|:---:|---|---:|---:|---:|---:|---:|---:|---|---|")
    for item in sorted(report.signals, key=lambda row: row.trade_date,
                       reverse=True)[:300]:
        out.append(
            f"| {item.trade_date} | {item.board_name} | {item.score:.1f} | "
            f"{LEVEL_TEXT.get(item.level.value, '—')} | "
            f"{'、'.join(item.triggered_dims) or '—'} | "
            f"{_fmt(item.change_pct)} | {_fmt(item.ret_5d)} | "
            f"{_fmt(item.ret_10d)} | {_fmt(item.ret_20d)} | "
            f"{_fmt(item.ret_60d)} | {_fmt(item.max_gain_pct)} | "
            f"{item.max_gain_date or '—'} | {'是' if item.confirmed else '否'} |")
    out.append("")
    out.append("> 各期涨幅单位 **%**，以**告警当日板块指数收盘价**为基准。")
    out.append("")
    return out


# ==================================================================
# 第五章
# ==================================================================


def _chapter_false_positives(report: BacktestReport) -> list[str]:
    out = ["## 第五章 · 假阳性分析", ""]
    items = report.false_positives
    if not items:
        out.append("本次回测没有「触发后未兑现」的告警。"
                   "注意：样本少时这条结论不稳健。")
        out.append("")
        return out
    out.append(f"共 {len(items)} 条告警在触发后 20 日内没有上涨、"
               "60 日内最大涨幅也未超过 5%。")
    out.append("")
    out.append("| 告警日 | 板块 | 预警分 | 等级 | 触发维度 | 20日 | 最大涨幅 | "
               "可能原因 |")
    out.append("|---|---|---:|:---:|---|---:|---:|---|")
    for item in items[:120]:
        out.append(f"| {item.trade_date} | {item.board_name} | {item.score:.1f} | "
                   f"{LEVEL_TEXT.get(item.level.value, '—')} | "
                   f"{'、'.join(item.triggered_dims) or '—'} | "
                   f"{_fmt(item.ret_20d)} | {_fmt(item.max_gain_pct)} | "
                   f"{_reason(item)} |")
    out.append("")
    summary = _fp_summary(items)
    if summary:
        out.append("**共性归因**")
        out.append("")
        for line in summary:
            out.append(f"- {line}")
        out.append("")
    return out


def _reason(item: Any) -> str:
    """按触发维度给一个**可复算**的归因（不做主观猜测）。"""
    dims = set(item.triggered_dims or [])
    if "volume_price" in dims and len(dims) <= 2:
        return "仅量价形态达标，缺资金面确认"
    if "macro" in dims and len(dims) <= 2:
        return "仅宏观维度达标，属共同项驱动（非板块独立异动）"
    if item.gate_bonus:
        return "有龙头共振但未兑现，可能为一日游行情"
    if len(dims) >= 4:
        return "多维度达标仍未兑现，需检查是否已在高位"
    return "单一维度驱动，缺共振确认"


def _fp_summary(items: Sequence[Any]) -> list[str]:
    """统计假阳性的维度构成与等级构成（给出可行动的改进方向）。"""
    out: list[str] = []
    counts: dict[str, int] = {}
    for item in items:
        for key in (item.triggered_dims or ["(无维度)"]):
            counts[key] = counts.get(key, 0) + 1
    if counts:
        top = sorted(counts.items(), key=lambda pair: -pair[1])[:5]
        out.append("假阳性告警中出现最多的维度："
                   + "、".join(f"{key}（{value} 次）" for key, value in top))
    levels: dict[str, int] = {}
    for item in items:
        levels[item.level.value] = levels.get(item.level.value, 0) + 1
    if levels:
        out.append("按等级分布：" + "、".join(
            f"{LEVEL_TEXT.get(key, key)} {value} 条"
            for key, value in sorted(levels.items())))
    no_resonance = sum(1 for item in items if not item.resonance)
    if no_resonance:
        out.append(f"其中 {no_resonance} 条**没有**龙头共振确认 —— "
                   "提高 `leader.resonance_ratio` 门槛或要求中信号必须共振，"
                   "可以直接减少这一类假阳性。")
    return out


# ==================================================================
# 第六章
# ==================================================================


def _chapter_correlation(report: BacktestReport) -> list[str]:
    corr = report.correlation
    out = ["## 第六章 · 因子相关性验证报告（去重效果检验）", ""]
    out.append(f"- 参与检验因子：{len(corr.factors)} 个")
    out.append(f"- 样本数：{corr.samples}")
    out.append(f"- 跨层相关性上限：**{corr.limit:g}**")
    out.append(f"- 跨层相关性｜均值｜：{_fmt(corr.cross_layer_mean_abs)}；"
               f"｜最大｜：{_fmt(corr.cross_layer_max_abs)}")
    out.append(f"- 结论：{'✅ ' if corr.passed else '⚠️ '}{corr.note}")
    out.append("")
    if not corr.matrix:
        out.append("相关性矩阵为空（有效因子不足）。")
        out.append("")
        return out
    if corr.high_pairs:
        out.append("**跨层高相关因子对（需正交化或删除）**")
        out.append("")
        out.append("| 因子 A | 因子 B | 相关系数 |")
        out.append("|---|---|---:|")
        for left, right, value in corr.high_pairs:
            out.append(f"| {FACTOR_LABELS.get(left, left)}（`{left}`） | "
                       f"{FACTOR_LABELS.get(right, right)}（`{right}`） | "
                       f"{value:.4f} |")
        out.append("")
    out.append("<details><summary>完整相关矩阵（点击展开）</summary>")
    out.append("")
    names = corr.factors
    out.append("| | " + " | ".join(_short(name) for name in names) + " |")
    out.append("|---|" + "---:|" * len(names))
    for left in names:
        cells = []
        for right in names:
            if left == right:
                cells.append("1")
            else:
                value = (corr.matrix.get(left, {}).get(right)
                         if names.index(right) > names.index(left)
                         else corr.matrix.get(right, {}).get(left))
                cells.append("—" if value is None else f"{value:.2f}")
        out.append(f"| {_short(left)} | " + " | ".join(cells) + " |")
    out.append("")
    out.append("</details>")
    out.append("")
    out.append("> 相关性算在**原始因子值**上（不是 0-100 的分位分）——"
               "分位映射会改变线性相关结构，用它验证去重会得出"
               "「相关性都很低」的假结论。跨层而非层内：层内高相关是正常的，"
               "重复加权的问题只出在跨层。")
    out.append("")
    return out


def _short(name: str) -> str:
    label = FACTOR_LABELS.get(name)
    return label.split("·")[-1] if label else name


# ==================================================================
# 缺口
# ==================================================================


def _chapter_gaps(report: BacktestReport) -> list[str]:
    out = ["## 附 · 数据缺口与口径说明", ""]
    if not report.gaps:
        out.append("本次回测没有记录到数据缺口。")
    else:
        for item in report.gaps:
            out.append(f"- {item}")
    out.append("")
    out.append("**已知的口径限制（与具体某次回测无关，长期成立）**")
    out.append("")
    out.append("- 板块资金流用**本地行情仓个股聚合**口径"
               "（Tushare 个股资金流），不是东财板块口径 —— "
               "东财 `moneyflow_ind_dc` 只有 2024 年起的数据，覆盖不了本回测区间。")
    out.append("- 北向资金用**个股持股数量变化**折算净买入；"
               "交易所自 2024-08 起停止披露日度个股净买入，"
               "该维度在 2024-08 之后退化为季度快照。")
    out.append("- 景气度只有**已实现盈余动量**（ROE / 净利 / 营收同比），"
               "未接入分析师一致预期。")
    out.append("- 概念板块成分股只有**当前快照**（`ths_member` 不带历史），"
               "概念口径的回测存在幸存者偏差；申万一级行业已用 "
               "`index_member_all` 的调入/调出日期做了 point-in-time 处理。")
    out.append("- 股东户数需**滚动累积**（`stk_holdernumber` 取不到历史快照），"
               "回测早期该子因子为空。")
    out.append("")
    return out


def _fmt(value: Any, *, pct: bool = False) -> str:
    """格式化数字；**None 渲染成"无数据"而不是 0**（见模块文档的写作纪律）。"""
    if value is None:
        return "无数据"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    return f"{number * 100:.2f}%" if pct else f"{number:.4f}"


__all__ = ["LEVEL_TEXT", "render"]
