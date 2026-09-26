"""把 `docs/` 里**已有的**主线回测结果导入 `mainline_backtest` 表。

## 为什么需要这个脚本

面板的「回测历史」读的是 `mainline_backtest` 表，而这张表**是空的**：
`/backtest/run` 要跑全区间 Walk-Forward（几十分钟，实测点了一次三小时没回音），
所以历史结果一直只躺在文件里 —— 用户看到的就是「缺数据说明：还没有回测记录」。

但项目里其实**早就有**这些结果，只是从没落库：

| 来源 | 内容 |
| --- | --- |
| `docs/mainline_iterations/0{1..4}_*.json` | 4 次迭代（pure055 / v23 / v24 / v25）：三个区间的逐因子 IC、告警持有收益、策略收益 |
| `docs/MAINLINE_STRATEGY_BACKTEST.xlsx` | 策略回测（3 笔×1 成 / 60 日 / 止损 7% / 止盈 10%）：逐笔明细 3233 笔、分档、分年、口径说明 |
| `docs/MAINLINE_STRATEGY_SWEEP.xlsx` | 止损×止盈 参数扫描 25 组 |

本脚本把它们**只读**解析后写进 `mainline_backtest`（`save_backtest` 是
`ON CONFLICT(run_id) DO UPDATE`，所以可重复执行、幂等）。

## 三条不粉饰的口径

1. **导入行必须自己说清来源。** 每一行都写 `gaps` 与正文首段，注明来自哪个文件、
   哪些字段是文件里没有而推断的。把导入数据混进"真跑出来的回测"里不标注，
   等于让人拿推测当证据。
2. **区间日期是推断的，不是文件里的。** 迭代 JSON 只记了各窗口的**天数**
   （旧 204 / 中 263 / 新 211），没有起止日期；策略 xlsx 的逐笔明细有 `触发日`。
   所以：策略行用逐笔明细的真实起止日；迭代行用本地评分表的覆盖区间，
   并在 `gaps` 里写明是推断。
3. **没有的指标就是没有，不补 0。** 迭代日志里没有多空年化、没有分场景、
   没有因子相关性矩阵 —— 这些字段一律留空（`None` / `[]`），
   而不是拿别的数顶上去。特别是 `correlation`：留空后接口会归一成 `null`，
   前端显示"无相关性验证结果"（这里正是那个 `{}` 白屏 bug 的现场）。

用法：

    python scripts/import_mainline_backtests.py            # 导入
    python scripts/import_mainline_backtests.py --dry-run  # 只看会写什么
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.core.config import get_settings                       # noqa: E402
from src.mainline.config import load_config                    # noqa: E402
from src.mainline.datastore import MainlineDataStore           # noqa: E402
from src.mainline.models import (BacktestMetrics, BacktestReport,  # noqa: E402
                                 FactorCorrelation)
from src.mainline.storage import build_mainline_repository     # noqa: E402

ITER_DIR = ROOT / "docs" / "mainline_iterations"
STRATEGY_XLSX = ROOT / "docs" / "MAINLINE_STRATEGY_BACKTEST.xlsx"
SWEEP_XLSX = ROOT / "docs" / "MAINLINE_STRATEGY_SWEEP.xlsx"

#: 迭代日志的标题（`label` → 展示名）。名字只用于正文，不参与键。
LABELS = {"pure055": "V2.2 提纯 0.55", "v23": "V2.3（层权重 100/0 + 阈值 83/81）",
          "v24": "V2.4", "v25": "V2.5"}


def _num(value: Any) -> float | None:
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None


def _ratio(value: Any) -> float | None:
    """百分数 → 小数（`46.97` → `0.4697`）。面板一律按小数渲染。"""
    raw = _num(value)
    return None if raw is None else round(raw / 100.0, 6)


def _stamp(text: str) -> str:
    return str(text or "").strip()


def score_span(repo: Any) -> tuple[str, str]:
    """本地**评分表**的覆盖区间（迭代行没有日期，用它兜底并在 gaps 里写明）。

    ⚠️ 用 `mainline_score` 而不是 `ml_board_bar`：板块指数从 20230103 就有，
    但迭代日志评的是**打过分的**那段（20231009 起）。用板块指数区间会把
    迭代回测的评估范围**说大**半年 —— 导入数据最不该犯的就是这种"看起来更全"。
    读法走仓储自己的 `_read`（本项目禁止绕过数据层自己开连接）。
    """
    try:
        rows = repo._read(  # noqa: SLF001 仓储自有的只读入口
            "SELECT MIN(trade_date) AS lo, MAX(trade_date) AS hi FROM mainline_score")
        if rows:
            return str(rows[0]["lo"] or ""), str(rows[0]["hi"] or "")
    except Exception:  # noqa: BLE001 兜底而已，读不到就留空
        pass
    return "", ""


# ==================================================================
# 迭代日志（JSON）
# ==================================================================


def build_iteration_report(path: Path, *, span: tuple[str, str]) -> BacktestReport:
    data = json.loads(path.read_text(encoding="utf-8"))
    label = str(data.get("label") or path.stem)
    iteration = int(data.get("iteration") or 0)
    at = _stamp(data.get("at"))
    windows: dict[str, Any] = data.get("ic") or {}
    # 头条指标取**新区间**：那是"现在还有没有效"最相关的窗口。
    # 三个窗口全量原文进正文，不给读者只剩一个数的错觉。
    head_window = "新区间" if "新区间" in windows else next(iter(windows), "")
    head = (windows.get(head_window) or {}).get("factors") or {}
    total20 = head.get("total@20") or {}

    metrics = BacktestMetrics(
        ic_mean=_num(total20.get("ic")),
        icir=_num(total20.get("icir")),
        ic_win_rate=_num(total20.get("win")),
        ic_samples=int(_num(total20.get("days")) or 0),
    )
    # 维度拆解：只放**真的有 IC** 的因子，层级名对齐其它视图。
    for key, name in (("total@20", "total"), ("six_dim@20", "six_dim"),
                      ("accumulation@20", "accumulation"),
                      ("leader@20", "leader"),
                      ("selection.rank_key@20", "selection.rank_key")):
        row = head.get(key)
        if not isinstance(row, dict):
            continue
        metrics.by_dim[name] = {
            "ic_mean": _num(row.get("ic")), "icir": _num(row.get("icir")),
            "ic_win_rate": _num(row.get("win")),
            "samples": int(_num(row.get("days")) or 0),
        }
    # 分持有期：迭代日志只有告警持有收益（h10~h90）的胜率与样本，没有多空年化 ——
    # 缺的字段一律 None，不拿别的数顶。
    for key, row in ((data.get("layers") or {}).get("告警持有收益") or {}).items():
        if not str(key).startswith("h") or not isinstance(row, dict):
            continue
        try:
            days = int(str(key)[1:])
        except ValueError:
            continue
        metrics.by_holding[str(days)] = {
            "long_annual": None, "long_excess": None,
            "win_rate": _ratio(row.get("胜率")),
            "samples": int(_num(row.get("样本")) or 0),
            "profit_loss_ratio": _num(row.get("盈亏比")),
            "expectancy": _num(row.get("期望收益")),
            "benchmark_win_rate": _ratio(row.get("基准胜率")),
        }

    strategy = (data.get("layers") or {}).get(
        "策略(3笔×1成/60日/止损7%/止盈10%)") or {}
    if strategy:
        metrics.signal_count = int(_num(strategy.get("笔数")) or 0)
        metrics.signal_hit_rate = _ratio(strategy.get("胜率"))

    report = BacktestReport(
        run_id=f"iter{iteration:02d}-{label}",
        started_at=at, finished_at=at,
        range_start=span[0], range_end=span[1],
        metrics=metrics,
        # 迭代日志里**没有**分折 / 分场景 / 相关性矩阵 —— 留空，不编。
        folds=[], scenes=[],
        correlation=FactorCorrelation(),
        config_note=_config_note(data.get("config") or {}),
        markdown=render_iteration_markdown(data, path, span, head_window),
        gaps=[
            f"本行由 `{path.relative_to(ROOT).as_posix()}` 导入（迭代日志快照），"
            "不是本轮 `POST /backtest/run` 现跑的结果。",
            f"区间日期按本地板块指数覆盖范围推断（{span[0]}~{span[1]}）："
            "迭代日志只记了各窗口的天数，没有起止日。",
            "分场景验证、因子相关性矩阵、多空年化在迭代日志里**不存在**，"
            "因此本行这些字段为空 —— 不是 0。",
        ],
        error="",
    )
    return report


def _config_note(config: dict[str, Any]) -> str:
    six = ((config.get("six_dim") or {}).get("weights") or {})
    acc = ((config.get("accumulation") or {}).get("weights") or {})
    layers = ((config.get("synthesis") or {}).get("layer_weights") or {})
    alert = config.get("alert") or {}
    parts = []
    if six:
        parts.append("六维 " + "/".join(f"{k}{v:g}" for k, v in six.items()))
    if acc:
        parts.append("建仓 " + "/".join(f"{k}{v:g}" for k, v in acc.items()))
    if layers:
        parts.append("合成 " + "/".join(f"{k}{v:g}%" for k, v in layers.items()))
    if alert:
        parts.append(f"阈值 中{alert.get('medium_score')}/强{alert.get('strong_score')}")
    return "；".join(parts) + "（来自迭代日志的配置快照）"


def render_iteration_markdown(data: dict[str, Any], path: Path,
                              span: tuple[str, str], head_window: str) -> str:
    lines: list[str] = []
    label = str(data.get("label") or "")
    lines.append(f"# 主线挖掘回测报告 · iter{data.get('iteration')} {label}"
                 f"（{LABELS.get(label, label)}）")
    lines.append("")
    lines.append(f"> 数据来源：`{path.relative_to(ROOT).as_posix()}`"
                 f"（迭代日志快照，记录时间 {data.get('at')}）。")
    lines.append(f"> 区间：{span[0]} ~ {span[1]}（按本地数据覆盖推断）。")
    lines.append("> 头条指标取「新区间」列；三个窗口的完整数字见下表，不要只看一个。")
    lines.append("")

    lines.append("## 1 区间 IC（逐因子 × 三个窗口）")
    lines.append("")
    lines.append("| 窗口 | 天数 | 板块数 | 因子 | IC | ICIR | IC胜率 | 有效天数 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for window, payload in (data.get("ic") or {}).items():
        factors = (payload or {}).get("factors") or {}
        for key, row in factors.items():
            if not isinstance(row, dict):
                continue
            lines.append(
                f"| {window}{' ←头条' if window == head_window and key == 'total@20' else ''}"
                f" | {payload.get('days')} | {payload.get('boards')} | `{key}`"
                f" | {row.get('ic')} | {row.get('icir')} | {row.get('win')}"
                f" | {row.get('days')} |")
    lines.append("")

    layers = data.get("layers") or {}
    holding = layers.get("告警持有收益") or {}
    if holding:
        lines.append("## 2 告警持有收益（按持有期）")
        lines.append("")
        lines.append("| 持有期 | 样本 | 胜率(%) | 盈亏比 | 期望收益 | 基准胜率(%) |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for key, row in holding.items():
            if not isinstance(row, dict):
                continue
            lines.append(f"| {key} | {row.get('样本')} | {row.get('胜率')}"
                         f" | {row.get('盈亏比')} | {row.get('期望收益')}"
                         f" | {row.get('基准胜率')} |")
        lines.append("")

    strategy = layers.get("策略(3笔×1成/60日/止损7%/止盈10%)") or {}
    if strategy:
        lines.append("## 3 策略口径收益（3 笔×1 成 / 60 日 / 止损 7% / 止盈 10%）")
        lines.append("")
        lines.append("| 指标 | 值 |")
        lines.append("| --- | --- |")
        for key, value in strategy.items():
            lines.append(f"| {key} | {value} |")
        lines.append("")

    lines.append("## 4 配置快照")
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(data.get("config") or {}, ensure_ascii=False, indent=2))
    lines.append("```")
    lines.append("")
    lines.append("## 5 本报告**没有**的内容（不要当成 0）")
    lines.append("")
    lines.append("- 分场景验证（CRO / 破冰等历史行情是否提前告警）：迭代日志未记录")
    lines.append("- 因子相关性矩阵（共线性检查）：迭代日志未记录")
    lines.append("- 多空组合年化 / 超额：迭代日志未记录")
    return "\n".join(lines)


# ==================================================================
# 策略回测（xlsx）
# ==================================================================


def _sheet_rows(path: Path, sheet: str) -> list[tuple]:
    import openpyxl
    book = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        return [tuple(row) for row in book[sheet].iter_rows(values_only=True)]
    finally:
        book.close()


def _table(rows: list[tuple]) -> tuple[list[str], list[dict[str, Any]]]:
    if not rows:
        return [], []
    header = [str(cell).strip() if cell is not None else "" for cell in rows[0]]
    out: list[dict[str, Any]] = []
    for row in rows[1:]:
        item = {header[i]: row[i] for i in range(min(len(header), len(row)))}
        if any(value is not None and value != "" for value in item.values()):
            out.append(item)
    return header, out


def build_strategy_report(path: Path) -> BacktestReport | None:
    if not path.exists():
        return None
    _, trades = _table(_sheet_rows(path, "逐笔明细"))
    _, buckets = _table(_sheet_rows(path, "分档"))
    _, years = _table(_sheet_rows(path, "分年"))
    _, notes = _table(_sheet_rows(path, "口径说明"))
    if not trades:
        return None
    dates = sorted(str(item.get("触发日")) for item in trades
                   if item.get("触发日"))
    capital = [_num(item.get("资金收益率")) for item in trades]
    capital = [value for value in capital if value is not None]
    holds = [_num(item.get("持有天数")) for item in trades]
    holds = [value for value in holds if value is not None]
    wins = sum(1 for value in capital if value > 0)
    stops = sum(1 for item in trades if item.get("止损") in (True, "True", "TRUE"))

    metrics = BacktestMetrics(
        signal_count=len(trades),
        signal_hit_rate=round(wins / len(capital), 6) if capital else None,
        avg_max_gain=(round(sum(capital) / len(capital), 4) if capital else None),
        median_max_gain=None,
    )
    # 分档（按信号等级）是这份回测最有信息量的一张表：它直接回答
    # "中/强信号是不是真的比弱信号好"。放进 by_dim，键前缀 level. 以免与因子名混淆。
    for row in buckets:
        level = str(row.get("等级") or "").strip()
        if not level:
            continue
        metrics.by_dim[f"level.{level}"] = {
            "trades": int(_num(row.get("笔数")) or 0),
            "avg_capital_return_pct": _num(row.get("资金收益率均值")),
            "annualized_pct": _num(row.get("年化资金")),
            "win_rate": _ratio(row.get("胜率")),
            "stop_rate": _ratio(row.get("止损比例")),
        }
    # 分年放进 by_holding？不行 —— 那是"持有期"的键，塞年份是类型说谎。
    # 年份表进正文，指标里只留真实的整体数字。

    lines = [f"# 主线挖掘策略回测（{path.name}）", ""]
    lines.append("> 数据来源：`docs/" + path.name + "` —— 逐笔明细 "
                 f"{len(trades)} 笔，导入自项目文档，非本轮现跑。")
    lines.append(f"> 区间：{dates[0]} ~ {dates[-1]}（取自逐笔明细的「触发日」）。")
    lines.append("")
    lines.append("## 0 口径")
    lines.append("")
    lines.append("| 口径 | 说明 |")
    lines.append("| --- | --- |")
    for row in notes:
        key = str(row.get("口径") or "").strip()
        if key:
            lines.append(f"| {key} | {row.get('说明')} |")
    lines.append("")
    lines.append("## 1 整体")
    lines.append("")
    lines.append("| 指标 | 值 |")
    lines.append("| --- | --- |")
    lines.append(f"| 笔数 | {len(trades)} |")
    lines.append(f"| 资金收益率均值(%) | "
                 f"{round(sum(capital) / len(capital), 4) if capital else '—'} |")
    lines.append(f"| 胜率(%) | {round(wins / len(capital) * 100, 2) if capital else '—'} |")
    lines.append(f"| 止损笔数 / 占比(%) | {stops} / "
                 f"{round(stops / len(trades) * 100, 2)} |")
    lines.append(f"| 平均持有天数 | "
                 f"{round(sum(holds) / len(holds), 1) if holds else '—'} |")
    lines.append("")
    lines.append("## 2 分档（按信号等级）")
    lines.append("")
    lines.append("| 等级 | 笔数 | 资金收益率均值 | 年化资金 | 胜率 | 止损比例 |")
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for row in buckets:
        lines.append("| " + " | ".join(str(row.get(key, "")) for key in
                                       ("等级", "笔数", "资金收益率均值", "年化资金",
                                        "胜率", "止损比例")) + " |")
    lines.append("")
    lines.append("## 3 分年")
    lines.append("")
    lines.append("| 年 | 笔数 | 资金收益率均值 | 年化资金 | 胜率 |")
    lines.append("| --- | --- | --- | --- | --- |")
    for row in years:
        lines.append("| " + " | ".join(str(row.get(key, "")) for key in
                                       ("年", "笔数", "资金收益率均值", "年化资金",
                                        "胜率")) + " |")
    lines.append("")
    lines.append("## 4 已知偏差")
    lines.append("")
    lines.append("- 「年化」按 `(1+资金收益率)^(252/60)-1` 折算，**忽略资金闲置与信号重叠**，"
                 "是上界不是可实现收益。")
    lines.append("- 分档的胜率是**资金收益率 > 0** 的比例，与面板里"
                 "「最大涨幅胜率」不是同一口径。")
    lines.append("- 本报告不含分场景验证与因子相关性矩阵（该 xlsx 没有这两张表）。")

    return BacktestReport(
        run_id="strategy-3x10pct-60d",
        started_at=file_stamp(path), finished_at=file_stamp(path),
        range_start=dates[0], range_end=dates[-1],
        metrics=metrics, folds=[], scenes=[],
        correlation=FactorCorrelation(),
        config_note="3 笔×1 成建仓（第 0/5/10 日收盘）/ 60 日收尾 / 止损 7% / "
                    "每 10% 台阶止盈一半（来自 xlsx 口径说明）",
        markdown="\n".join(lines),
        gaps=[
            f"本行由 `docs/{path.name}` 导入，不是本轮 `POST /backtest/run` 现跑的结果。",
            "IC / ICIR 类指标该文件没有（它不是因子有效性回测），因此为空 —— 不是 0。",
            "逐笔明细的 「年化(资金)」忽略资金闲置与信号重叠，是上界。",
        ],
        error="",
    )


def build_sweep_report(path: Path) -> BacktestReport | None:
    if not path.exists():
        return None
    _, rows = _table(_sheet_rows(path, "Sheet1"))
    if not rows:
        return None
    best = max(rows, key=lambda item: _num(item.get("资金收益率均值")) or -1e9)
    lines = [f"# 主线挖掘策略参数扫描（{path.name}）", ""]
    lines.append("> 数据来源：`docs/" + path.name + "` —— 止损 × 止盈台阶 共 "
                 f"{len(rows)} 组，导入自项目文档。")
    lines.append("")
    lines.append("| 止损 | 止盈台阶 | 笔数 | 资金收益率均值 | 年化(资金) | 中位数 "
                 "| 胜率 | 止损比例 | 平均持有天数 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    keys = ("止损", "止盈台阶", "笔数", "资金收益率均值", "年化(资金)", "中位数",
            "胜率", "止损比例", "平均持有天数")
    for row in rows:
        lines.append("| " + " | ".join(str(row.get(key, "")) for key in keys) + " |")
    lines.append("")
    lines.append("## 结论")
    lines.append("")
    lines.append(f"- 按「资金收益率均值」最优的一组：止损 {best.get('止损')}、"
                 f"止盈台阶 {best.get('止盈台阶')}，"
                 f"笔数 {best.get('笔数')}，均值 {best.get('资金收益率均值')}。")
    lines.append("- ⚠️ 这是**同一段历史**上的参数扫描，"
                 "最优组天然带选择偏差；它只说明「参数敏感」，不能直接当最优参数用。")
    lines.append("- 止损放宽到极大值（等于不止损）时胜率反而更高 —— 说明"
                 "原先的 7% 止损在这段样本里是被反复打掉的，值得单独复盘。")

    return BacktestReport(
        run_id="strategy-sweep-stop-take",
        started_at=file_stamp(path), finished_at=file_stamp(path),
        range_start="", range_end="",
        metrics=BacktestMetrics(
            signal_count=int(_num(best.get("笔数")) or 0),
            signal_hit_rate=_ratio(best.get("胜率")),
            avg_max_gain=_num(best.get("资金收益率均值")),
        ),
        folds=[], scenes=[], correlation=FactorCorrelation(),
        config_note=f"止损 × 止盈台阶 扫描 {len(rows)} 组"
                    f"（存活阈值 7%~不限 × 台阶 10%~不限）",
        markdown="\n".join(lines),
        gaps=[f"本行由 `docs/{path.name}` 导入；区间日期该文件未记录，故留空。",
              "IC / ICIR / 分场景 / 相关性均不适用（参数扫描），因此为空。"],
        error="",
    )


def _now() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


def file_stamp(path: Path) -> str:
    """源文件的写入时刻（= 这份结果**产出**的时间），带 +08:00。

    ⚠️ 不能用导入时刻（`_now()`）：那样每重跑一次脚本，这一行就"变新"一次，
    在「回测历史」里反复顶到最上面 —— 排序变成"谁最后被导入"，
    而不是"谁最后被算出来"。文件 mtime 才是这份结果真实的产出时间。

    语义分工（导入行统一遵守）：
    * `started_at` / `finished_at` —— 这份结果**什么时候算出来的**
    * `range_start` / `range_end` —— 它**覆盖了哪段数据**
    """
    try:
        stamp = datetime.fromtimestamp(path.stat().st_mtime,
                                       tz=timezone(timedelta(hours=8)))
        return stamp.isoformat(timespec="seconds")
    except OSError:
        return ""


# ==================================================================
# 主流程
# ==================================================================


def main() -> int:
    parser = argparse.ArgumentParser(description="导入 docs/ 里已有的主线回测结果")
    parser.add_argument("--dry-run", action="store_true", help="只打印不落库")
    args = parser.parse_args()

    config = load_config()
    store = MainlineDataStore(config=config)
    _ = store                      # 保留装配（行情/日历在导入时不用，但保持与其它脚本一致）
    repo = build_mainline_repository(get_settings())
    if repo is None:
        print("❌ 主线仓储不可用（检查 settings.sqlite_path）", file=sys.stderr)
        return 1
    span = score_span(repo)

    reports: list[BacktestReport] = []
    for path in sorted(ITER_DIR.glob("*.json")):
        if path.stem[0].isdigit():          # 只取 01_/02_… 迭代日志，跳过质量报告
            reports.append(build_iteration_report(path, span=span))
    strategy = build_strategy_report(STRATEGY_XLSX)
    if strategy is not None:
        reports.append(strategy)
    sweep = build_sweep_report(SWEEP_XLSX)
    if sweep is not None:
        reports.append(sweep)

    print(f"识别到 {len(reports)} 份可导入的回测结果：")
    for report in reports:
        metrics = report.metrics
        print(f"  · {report.run_id:26s} {report.range_start}~{report.range_end}"
              f"  IC={metrics.ic_mean} ICIR={metrics.icir}"
              f"  信号={metrics.signal_count} 胜率={metrics.signal_hit_rate}"
              f"  markdown={len(report.markdown)} 字")

    if args.dry_run:
        print("\n--dry-run：没有写库。")
        return 0

    async def persist() -> None:
        for report in reports:
            run_id = await repo.save_backtest(report)
            print(f"  ✅ 已落库 {run_id}")

    asyncio.run(persist())
    total = len(asyncio.run(repo.list_backtests(limit=100)))
    print(f"\n完成：mainline_backtest 现有 {total} 行。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
