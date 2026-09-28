"""采集深度权威表的护栏。

## 用户裁定（2026-09-28）

> 「采集路径的条数上限默认设置为60，具体的细分如下」+ 14 条细分类别

## 本文件钉住四件事

1. **默认 60** —— 用户明确裁定，不许漂移
2. **具体优先于一般** —— 细分表里有重叠（宏观/另类 10 · 宏观月度 12 · 宏观季度 8），
   所以规则**有序**，先匹配者胜。用无序 dict 会让插入序静默决定结果。
3. **截断方向是"最新 N 条"** —— 写反了会静默给出十年前的数据
4. **不依赖调用方顺序** —— 连接器有的升序有的降序，同一个 `[:N]` 含义相反
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.infrastructure.catalog import fetch_depth as fd  # noqa: E402


# ============================================================
# ① 默认 60
# ============================================================


def test_default_depth_is_sixty():
    """用户裁定：默认 60。"""
    assert fd.DEFAULT_DEPTH == 60


def test_unknown_indicator_falls_back_to_default():
    assert fd.depth_for("完全不认识的指标:xyz") == 60
    assert fd.category_of("完全不认识的指标:xyz") == "默认"


# ============================================================
# ② 具体优先于一般（**顺序即语义**）
# ============================================================


def test_specific_rules_win_over_general():
    """★ 核心：宏观的 8/12/10 三档必须按**具体度**生效，不是按字典序。

    `GDP` 命中季度(8) 而不该落月度(12) 或另类(10)；
    `CPI` 命中月度(12) 而不该落另类(10)。
    """
    assert fd.depth_for("GDP") == 8, "GDP 应命中「宏观季度数据」8 条"
    assert fd.depth_for("CPI") == 12, "CPI 应命中「宏观月度数据」12 条"
    assert fd.depth_for("PPI") == 12
    assert fd.depth_for("M2") == 12
    assert fd.depth_for("us_cpi_yoy") == 10, "us_* 应命中「宏观/另类」10 条"
    assert fd.depth_for("fed:rate_prob:next") == 10


def test_category_names_are_explainable():
    """每个命中都要能说出**为什么是这个数**（可 review）。"""
    assert fd.category_of("CPI") == "宏观月度数据"
    assert fd.category_of("us_nonfarm") == "宏观/另类数据"
    assert fd.category_of("idx_val:snapshot:all") == "指数快照类"
    assert fd.category_of("PE(TTM):600036") == "结构化行情/财务"
    assert fd.category_of("stock_close:600036") == "日线K线"


def test_user_table_values_are_all_present():
    """用户细分表里的数字必须都在表里（防漏抄）。"""
    required = {
        "宏观/另类数据": 10,
        "研报/公告检索": 15,
        "结构化行情/财务": 100,
        "指数快照类": 20,
        "日线K线": 15,
        "分钟级行情": 60,
        "研报/公告文本": 30,
        "宏观月度数据": 12,
        "宏观季度数据": 8,
    }
    for cat, want in required.items():
        assert fd.CATEGORY_TABLE.get(cat) == want, (
            f"{cat} 应为 {want}，实际 {fd.CATEGORY_TABLE.get(cat)}")


def test_entity_count_categories_are_not_treated_as_row_counts():
    """「7 只标的 / 22 个维度 / 100 个标的」不是**条数**语义，本模块不截断。

    把它们混进条数表会导致"一次只分析 7 只"这类静默错误。
    """
    for name, n in fd.COUNT_BY_ENTITY.items():
        assert n > 0
        assert name not in fd.CATEGORY_TABLE, (
            f"{name} 是「按只/按维度」计数，不该进条数表")


# ============================================================
# ③ 截断方向 = 最新 N 条（**最容易写反的地方**）
# ============================================================


def _pts(dates: list[str]) -> list[dict]:
    return [{"period_date": d, "value": i} for i, d in enumerate(dates)]


def test_trim_keeps_the_newest_not_the_oldest():
    """★ 保留**最新** N 条 —— 写反了会静默给出十年前的数据。"""
    points = _pts([f"20{20 + i}-01-01" for i in range(30)])   # 2020..2049
    out = fd.trim(points, "CPI")                              # 月度 → 12 条
    assert len(out) == 12
    assert out[-1]["period_date"] == "2049-01-01", "尾部应是最新"
    assert out[0]["period_date"] == "2038-01-01", "12 条应覆盖 2038..2049"


def test_trim_is_order_independent():
    """★ 不依赖调用方顺序：升序 / 降序 / 乱序，结果必须相同。

    实测风险：`smart_fetch.py` 原实现 `points[:N]` 在升序连接器上
    取到的是**最旧的 N 条**，而降序连接器上是**最新 N 条** —— 同名不同义。
    """
    asc = _pts([f"20{20 + i}-01-01" for i in range(30)])
    desc = list(reversed(asc))
    shuffled = [asc[i] for i in (5, 0, 29, 12, 3, 20, 8, 1, 17, 25,
                                 2, 9, 14, 27, 6, 19, 4, 22, 11, 28,
                                 7, 13, 24, 10, 16, 21, 15, 18, 23, 26)]
    a, d, s = (fd.trim(x, "CPI") for x in (asc, desc, shuffled))
    assert [p["period_date"] for p in a] == [p["period_date"] for p in d]
    assert [p["period_date"] for p in a] == [p["period_date"] for p in s]


def test_trim_is_noop_when_under_limit():
    points = _pts(["2026-01-01", "2026-02-01"])
    assert fd.trim(points, "CPI") == points


def test_trim_handles_datapoint_objects():
    """真实链路传的是 DataPoint（有 .period_date），也要能排。"""

    class P:
        def __init__(self, d: str) -> None:
            self.period_date = d

    points = [P(f"20{20 + i}-01-01") for i in range(30)]
    out = fd.trim(points, "CPI")
    assert len(out) == 12
    assert out[-1].period_date == "2049-01-01"


def test_trim_without_dates_does_not_guess():
    """缺 period_date 时退化：不排序、只取尾部 —— 不猜，也不静默改语义。"""
    points = [{"value": i} for i in range(30)]
    out = fd.trim(points, "CPI")
    assert len(out) == 12
    assert out[-1]["value"] == 29


# ============================================================
# ④ 全量拉取类不截断
# ============================================================


def test_full_fetch_is_not_trimmed():
    points = _pts([f"2026-01-{i:02d}" for i in range(1, 29)])
    for ind in ("news_poll", "intel_incremental"):
        assert fd.is_full_fetch(ind), ind
        assert fd.trim(points, ind) == points


def test_public_indicators_are_not_full_fetch():
    """反向：普通指标不许被误判为全量（否则上限形同虚设）。"""
    for ind in ("CPI", "PE(TTM):600036", "idx_val:snapshot:all"):
        assert not fd.is_full_fetch(ind), ind


# ============================================================
# ⑤ 接线：改了表但没人调 = 没修（本项目反复踩过的坑）
# ============================================================


def test_smart_fetcher_actually_uses_the_depth_table():
    """★ 跨模块接线判据：`smart_fetch.py` 必须调用 `fetch_depth.trim`。

    为什么单测 `fetch_depth` 证明不了：它全绿，而 SmartFetcher 可能还在用
    老的 `points[:limit]`。本项目实测过同款（"改了 gateway 但 planner 不传预算"）。
    """
    src = (ROOT / "src" / "infrastructure" / "catalog" / "smart_fetch.py"
           ).read_text(encoding="utf-8")
    assert "fetch_depth" in src, (
        "smart_fetch 没有引用 fetch_depth —— 采集深度表形同虚设")
    assert "trim" in src, "smart_fetch 没有调用 trim()"


def test_old_first_n_slice_is_gone():
    """★ 老的 `points[:limit_per_indicator]` 切片必须已被移除。

    它是"前 N 条"语义 —— 在升序连接器上取到**最旧的 N 条**，
    与本表的"最新 N 条"相反。留着它 = 两套语义并存，必有一处反了。

    ⚠️ 用 `ast` 找**真实的切片表达式**，不用字符串匹配：
    实测踩过 —— 字符串版匹配到了本文件/源码里**说明这件事的注释**
    （`# 原来这里是 points[:limit_per_indicator]`），报了一个假失败。
    """
    import ast

    tree = ast.parse((ROOT / "src" / "infrastructure" / "catalog"
                      / "smart_fetch.py").read_text(encoding="utf-8"))
    offenders: list[int] = []
    for node in ast.walk(tree):
        # 找 `<x>[:limit_per_indicator]`
        if not isinstance(node, ast.Subscript):
            continue
        sl = node.slice
        if not isinstance(sl, ast.Slice) or sl.upper is None:
            continue
        up = sl.upper
        if isinstance(up, ast.Name) and up.id == "limit_per_indicator":
            offenders.append(node.lineno)
    assert not offenders, (
        f"smart_fetch.py 第 {offenders} 行仍有「前 N 条」切片 —— "
        "与 fetch_depth 的「最新 N 条」语义冲突")
