"""题材准入：按历史回测的 **20 日胜率**剔除「判得准的差题材」。

## 它解决什么问题

池子里 138 个概念，有些**反复触发却反复不兑现**（创新药 21 个已走满窗口里只有
24% 为正、CRO概念 23 个里 39%），它们持续贡献假阳性，把真信号的位置占掉。
2026-09-25 用户口径：

    「根据历史回测的每个概念的总胜率，超过 40% 的列一个清单，
      把题材池不在清单的概念清理掉……只保留预测准确率大于 40% 的题材。」

## 判据：**严格大于**门槛，且**样本要够**

    exclude = win_rate_20d ≤ threshold  **且** done_20d ≥ min_samples

`win_rate_20d` = 该板块**已走满**的 20 日窗口里「窗口末收盘 > 信号日收盘」的比例
（与「回测收益展示」面板、`summarize_boards` 同一处口径）。

⚠️ **判不准的留观察，不剔除。** 用户在三个方案里选了这一侧，理由是：
一条 20 日窗口都没走满的题材（新概念）谈不上「准确率低」；只走过 1~2 个窗口的
题材，胜率是抛硬币级别的噪声 —— 而它们本来也几乎不产生告警
（本机实测那 8 个加起来只有 11 个信号周期 / 14 条告警）。
一起剔掉等于让系统**永远看不到刚冒头的新题材**，而「抓新主线」正是这个模块
存在的理由。所以 `select()` 把它们放进观察名单：留在池内、继续攒证据。

## 这份清单是**池级**剔除（与 `sector_blacklist.yaml` 同一语义）

被剔除的题材：不进打分池 → 不打分 → 不产生告警 → 不再同步行情。
`store.boards()` 每次现读生成文件（按 mtime 失效），所以改完**不必重启**，
下一轮打分即生效。要恢复某个题材：重新生成时把它排除掉，或调低门槛。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta, timezone
from typing import Any

#: 胜率门槛（**池级取舍**）：**严格大于**它才保留。
#:
#: ⚠️ **不再与 `alert_returns.DEFAULT_MIN_WIN_RATE` 同值**（2026-09-30）：那个是
#: **展示口径**（"这次给不给你看"，同年 09-30 放宽到 0.39），这个是**池级取舍**
#: （"这个题材还做不做"）。**跟着一起降会造成静默缩水**：`select()` 会把胜率落在
#: (0.39, 0.40] 的题材也判成"该剔"，而它们不在冻结名单内 ⇒ 下次生成清单 + 同步
#: 之后池子会少几个题材，且 `scripts/build_theme_exclusions.py --keep` 也留不住
#: （`--keep` 只在**已经**被判为剔除时才生效）。改这个数=改池子，必须显式。
#: 判据：`tests/unit/test_mainline_alert_returns.py::test_display_gate_is_decoupled_from_pool_gate`。
DEFAULT_THRESHOLD = 0.40
#: 至少要有几个「已走满的 20 日窗口」才有资格被剔除；低于它的进观察名单。
DEFAULT_MIN_SAMPLES = 3
#: 统计**哪些档位**的信号。
#:
#: ⚠️ 这里刻意是**全部三档**，而「回测收益展示」面板只看 `strong/medium` ——
#: 两者口径不同是**有意的**，因为问的不是同一件事：
#:
#: * 面板的 `strong/medium` 是**展示范围**的选择（它写明"弱信号不进入本面板"），
#:   回答"这个题材推给我的强/中信号准不准"；
#: * 这里是**池级取舍**：要不要继续挖这个题材。那就该看它**产生过的全部信号**
#:   （含 900 条弱信号）—— 一个只会反复报弱信号、且从不兑现的题材，
#:   同样在占位置。
#:
#: 本机实测两种口径的差别很小（剔 16 vs 剔 18），但边界题材会换人：
#: 只按强/中算会剔掉智能音箱/数据中心(AIDC)/商业航天/煤化工概念，
#: 按全部档位算则剔掉生物疫苗/医美概念。换口径要显式用 `--levels`。
DEFAULT_LEVELS: tuple[str, ...] = ("strong", "medium", "weak")
#: 生成文件（`configs/` 下）。
DEFAULT_FILENAME = "mainline_theme_exclusions.yaml"
#: 生成文件的格式版本；结构变了才 +1。
FORMAT_VERSION = 1


def _num(value: Any) -> float | None:
    """转 float；`None` / 空串 / 非数值一律 `None`（不退化成 0）。"""
    if value is None or value == "":
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out


def select(boards: Sequence[dict[str, Any]], *,
           threshold: float = DEFAULT_THRESHOLD,
           min_samples: int = DEFAULT_MIN_SAMPLES
           ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """把板块汇总分成 `(剔除清单, 观察名单)`。

    纯函数，不碰文件也不碰数据库 —— 判据只能有一处，生成脚本与单元测试都调它。

    `boards` 是 `alert_returns.summarize_boards()` 的输出。**池内但一条告警都没有**
    的题材不在这里（它们连胜率都没有），由调用方补进观察名单。
    """
    excluded: list[dict[str, Any]] = []
    watching: list[dict[str, Any]] = []
    for item in boards:
        rate = _num(item.get("win_rate_20d"))
        done = int(item.get("done_20d") or 0)
        if rate is None or done < int(min_samples):
            watching.append(item)
        elif rate <= threshold:
            excluded.append(item)
    # 剔除清单按胜率升序（最差的排最前），人读台账时先看到最该剔的；
    # 同胜率按窗口数降序 —— 样本越多的结论越硬，越该排前面。
    excluded.sort(key=lambda item: (_num(item.get("win_rate_20d")) or 0.0,
                                    -int(item.get("done_20d") or 0)))
    watching.sort(key=lambda item: (int(item.get("done_20d") or 0),
                                    _num(item.get("win_rate_20d")) or 0.0))
    return excluded, watching


def watch_reason(item: dict[str, Any], min_samples: int) -> str:
    """观察名单里每一条为什么没被剔 —— 台账必须能自己回答这句话。"""
    if _num(item.get("win_rate_20d")) is None:
        return "没有任何已走满的 20 日窗口，胜率判不出来（新题材）"
    return (f"已走满窗口只有 {int(item.get('done_20d') or 0)} 个"
            f"（少于 {min_samples} 个），胜率不可信")


def _quote(text: Any) -> str:
    """YAML 双引号标量：内部引号必须转义，否则整份文件会解析失败。"""
    return str(text or "").replace("\\", "\\\\").replace('"', '\\"')


def _entry_line(item: dict[str, Any]) -> str:
    """一条台账记录（YAML 流式映射，与 `sector_blacklist.yaml` 同构）。"""
    rate = _num(item.get("win_rate_20d"))
    avg = _num(item.get("avg_ret_20d"))
    return (f'  - {{ code: "{_quote(item.get("board_code"))}",'
            f' name: "{_quote(item.get("board_name"))}",'
            f' win_rate_20d: {"null" if rate is None else f"{rate:.4f}"},'
            f' done_20d: {int(item.get("done_20d") or 0)},'
            f' signals: {int(item.get("signals") or 0)},'
            f' alerts: {int(item.get("alert_total") or 0)},'
            f' avg_ret_20d: {"null" if avg is None else f"{avg:.2f}"} }}')


def render(excluded: Sequence[dict[str, Any]], watching: Sequence[dict[str, Any]], *,
           threshold: float, min_samples: int, pool_size: int, as_of: str,
           levels: Sequence[str], now: str = "") -> str:
    """生成台账文件全文（这份文件是 `boards()` 唯一读的东西）。"""
    stamp = now or datetime.now(timezone(timedelta(hours=8))).isoformat(
        timespec="seconds")
    kept = int(pool_size) - len(excluded)
    level_text = "/".join(str(item) for item in levels)
    head = [
        "# 主线题材剔除清单（由 `scripts/build_theme_exclusions.py` **自动生成**）",
        "#",
        "# ## 谁会被写进这份清单",
        "#",
        f"#     剔除 = 20 日胜率 <= {threshold:.0%}  **且** 已走满的 20 日窗口 >= {min_samples} 个",
        "#",
        "# 胜率 = 该题材**已走满**的 20 日窗口里「窗口末收盘 > 信号日收盘」的比例",
        f"# （统计档位：{level_text}）。",
        "# 「已走满的 20 日窗口 + 期末收盘 vs 信号日收盘」这一步与「回测收益展示」",
        "# 面板是同一处实现，但**档位范围**刻意不同：面板只看 strong/medium",
        "# （它的注释写明「弱信号不进入本面板」），而池级取舍要看这个题材",
        "# **产生过的全部信号** —— 只会反复报弱信号且从不兑现的题材同样在占位置。",
        "# 门槛是**严格大于**：恰好等于门槛也算不达标。",
        "#",
        "# ⚠️ **判不准的不在这里**：一条 20 日窗口都没走满的（新概念）、",
        f"# 或窗口少于 {min_samples} 个的，都留在池内继续攒证据 —— 见文件末尾的 watching。",
        "# 把「判不出」当成「不通过」，等于让系统永远看不到刚冒头的新题材。",
        "#",
        "# ## 生效方式（池级剔除）",
        "#",
        "# 与 `sector_blacklist.yaml` **同一套语义**：不在打分池 → 不打分 →",
        "# 不产生告警 → 不再同步行情。`store.boards()` 每次现读本文件（按 mtime 失效），",
        "# 所以改完**不需要重启**，下一轮打分即生效。",
        "#",
        "# ## 怎么改 / 怎么恢复某个题材",
        "#",
        "# **不要手工编辑本文件** —— 下次生成会覆盖。要改就改生成脚本的参数：",
        "#   python scripts/build_theme_exclusions.py --threshold 0.45 --min-samples 5",
        "#   python scripts/build_theme_exclusions.py --keep 886015.TI   # 单独放过一个",
        "#",
        f"# 本次：池内 {pool_size} 个 → 剔除 {len(excluded)} 个、留 {kept} 个"
        f"（其中 {len(watching)} 个在观察名单）。",
        "",
        f"version: {FORMAT_VERSION}",
        f'generated_at: "{stamp}"',
        f'as_of: "{as_of}"',
        "rule:",
        "  metric: win_rate_20d",
        f"  threshold: {threshold}",
        f"  min_samples: {min_samples}",
        f"  levels: [{', '.join(str(item) for item in levels)}]",
        "counts:",
        f"  pool: {pool_size}",
        f"  excluded: {len(excluded)}",
        f"  kept: {kept}",
        f"  watching: {len(watching)}",
        "#",
        "# `codes:` 是**唯一**被代码读取的键（`load_theme_exclusions` 复用",
        "# sector_blacklist 的解析器），每行后面挂的是判定依据，供人核对。",
    ]
    if excluded:
        head.append("codes:")
        head.extend(f"  # 胜率 {_num(item.get('win_rate_20d')) or 0:.0%}"
                    f" · 已走满 {int(item.get('done_20d') or 0)} 个窗口"
                    f" · 20 日均实际收益 {_num(item.get('avg_ret_20d'))}"
                    for item in excluded)
        head.extend(_entry_line(item) for item in excluded)
    else:
        head.append("codes: []")

    tail = ["",
            "# 观察名单：**仍在池内**，只是暂时判不准。只作台账，代码不读这一段。"]
    if watching:
        tail.append("watching:")
        for item in watching:
            tail.append(f"  # {watch_reason(item, min_samples)}")
            tail.append(_entry_line(item))
    else:
        tail.append("watching: []")
    return "\n".join(head + tail) + "\n"


__all__ = ["DEFAULT_FILENAME", "DEFAULT_LEVELS", "DEFAULT_MIN_SAMPLES",
           "DEFAULT_THRESHOLD", "FORMAT_VERSION", "render", "select",
           "watch_reason"]
