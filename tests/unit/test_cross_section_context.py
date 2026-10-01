"""★★★ 横截面指标的**维度还原**与**可复现截断**护栏。

## 这个文件守的是哪两件事（都是实测现场，不是假想）

`ind:sw_third_pe_ttm:all` / `ind:sw_third_dividend_yield:all` 这类 `:all` 指标是
**横截面**，不是时间序列：实测 335 条，`period_date` **全部等于 2026-09-29**，
每条属于一个不同的申万三级行业（`extra.industry_name`）。

而 `_build_context()` 原先的渲染是 `- {指标} {期}={值} c{置信} {来源}`，
**不带 `extra`**。于是模型收到的是 60 行一模一样的
`ind:sw_third_pe_ttm:all 2026-09-29=80.53`，**不知道哪个数是哪个行业** ——
不报错，只是模型随手挑一个当"该行业 PE"，或者编一个。

第二个更隐蔽：`context_max_periods = 60` 把它当"最近 60 **期**"截断，
可这里根本没有"期" —— 同一个日期上的 335 个成员被**任意**留下 60 个。
SQL 返回顺序不稳定 ⇒ **两次运行给出不同子集，同一个问题两次答案不一样**，
而且无法复现。

## 判据纪律

- 断言**配对**（哪个值配哪个标签），不是"上下文里出现了『银行业』"
  —— 后者在标签与值错位时照样通过（本项目实测过同类假绿）。
- 断言**打乱输入顺序后输出逐字节相同**（可复现性），而不是"看起来有序"。
- 断言**截断是显式的**（头部写出总数与"已截断"），
  而不是"展示了 60 行" —— 静默截断正是本文件要防的失败模式。
- **反面也要测**：普通时间序列不许被当成横截面、不许插入标签；
  `fed:policy_range` 那种"同一天多条但**没有任何字段能区分**"的情形
  **必须仍被当成缺陷暴露**，不能被"横截面"这个标签掩盖。
"""

from __future__ import annotations

import random

from src.domain.agents.analysis.base import AnalysisAgentBase


class _Probe(AnalysisAgentBase):
    """最小可用的 Agent 替身：只用到 `_build_context` 与 `context_max_periods`。"""

    context_max_periods = 60

    def __init__(self, max_periods: int | None = 60) -> None:
        super().__init__("A_probe", gateway=None)  # type: ignore[arg-type]
        self.context_max_periods = max_periods

    def _requirements(self, payload):  # pragma: no cover - 本文件不测 LLM 输出
        return ""

    def get_capabilities(self) -> dict:  # pragma: no cover - BaseAgent 抽象方法
        return {"agent_id": self.agent_id, "capabilities": [], "task_tier": self.task_tier}

    def health_check(self) -> bool:  # pragma: no cover - BaseAgent 抽象方法
        return True


def _payload(points: list[dict]) -> object:
    from src.domain.agents.analysis.base import AnalysisPayload

    return AnalysisPayload(data_points=points, focus="银行")


def _member(indicator: str, value, industry: str, date: str = "2026-09-29") -> dict:
    return {
        "indicator": indicator,
        "value": value,
        "period_date": date,
        "source_name": "AKShare申万行业估值",
        "confidence": 0.95,
        "extra": {"industry_name": industry, "industry_code": f"{hash(industry) & 0xFFFF}.SI"},
    }


def _cross_section(n: int, indicator: str = "ind:sw_third_pe_ttm:all") -> list[dict]:
    return [_member(indicator, 10.0 + i, f"行业{i:03d}") for i in range(n)]


# ============================================================
# ① 维度还原：每个值必须能对上是"谁的"
# ============================================================


def test_cross_section_members_are_labelled_and_paired():
    """★★★ 每个数值行都必须带上它自己的成员标签（配对断言，不是出现断言）。"""
    points = [_member("ind:sw_third_pe_ttm:all", 80.53, "种植业"),
              _member("ind:sw_third_pe_ttm:all", 25.34, "粮食种植"),
              _member("ind:sw_third_pe_ttm:all", 12.5, "其他种植业")]
    ctx = _Probe()._build_context(_payload(points))  # noqa: SLF001

    for value, industry in ((80.53, "种植业"), (25.34, "粮食种植"),
                            (12.5, "其他种植业")):
        paired = [ln for ln in ctx.splitlines()
                  if f"={value}" in ln and f"[{industry}]" in ln]
        assert paired, (
            f"没有一行同时含「{value}」与「[{industry}]」—— 值与其成员标签**错位或缺失**。\n"
            f"上下文：\n{ctx}"
        )


def test_cross_section_header_states_member_count_and_dimension():
    """头部必须说清"这是横截面、同日多少成员、按哪个字段区分"。"""
    ctx = _Probe()._build_context(_payload(_cross_section(5)))  # noqa: SLF001
    assert "横截面" in ctx
    assert "同日 5 个成员" in ctx
    assert "industry_name" in ctx
    assert "不是时间序列" in ctx


# ============================================================
# ② 可复现：输入顺序不许影响输出
# ============================================================


def test_output_is_reproducible_under_input_shuffle():
    """★★★ 打乱输入顺序 → 输出**逐字节相同**。

    这是"两次运行给出不同子集"那个缺陷的机器复现：
    335 个同日成员 + `context_max_periods=60`，若排序只按 `period_date`
    （全部相等 ⇒ 稳定排序保留原顺序），留下哪 60 个就取决于 SQL 返回顺序。
    """
    points = _cross_section(120)
    base = _Probe()._build_context(_payload(list(points)))  # noqa: SLF001
    for seed in (1, 7, 99):
        shuffled = list(points)
        random.Random(seed).shuffle(shuffled)
        again = _Probe()._build_context(_payload(shuffled))  # noqa: SLF001
        assert again == base, (
            f"打乱输入（seed={seed}）后输出变了 —— 截断留下的子集不可复现。\n"
            "→ 横截面必须按 (期 desc, 值 desc, 维度标签) 全序排序。"
        )


def test_truncation_is_explicit_not_silent():
    """★★ 335 个成员只展示 60 条时，头部必须**明写已截断**（不许静默）。"""
    ctx = _Probe(max_periods=60)._build_context(  # noqa: SLF001
        _payload(_cross_section(335)))
    assert "同日 335 个成员" in ctx
    assert "展示 60 条" in ctx
    assert "已截断" in ctx


def test_small_cross_section_is_marked_full_not_truncated():
    """★ 反面：本来就全展示了，不许谎称"已截断"（两个方向都要对）。"""
    ctx = _Probe(max_periods=60)._build_context(_payload(_cross_section(4)))  # noqa: SLF001
    assert "（全量）" in ctx
    assert "已截断" not in ctx


# ============================================================
# ③ 反面：时间序列不许被当成横截面
# ============================================================


def test_time_series_is_not_treated_as_cross_section():
    """★★ 普通时间序列（各期一个值）不许插入成员标签、不许出现横截面头部。

    这一条防的是"过度矫正"：把按期的序列也加上 `[标签]`，
    模型会以为每个数都属于某个成员，反而更容易错。
    """
    points = [
        {"indicator": "CPI", "value": 0.8, "period_date": p,
         "source_name": "国家统计局", "confidence": 0.9}
        for p in ("2026-07-01", "2026-08-01", "2026-09-01")
    ]
    ctx = _Probe()._build_context(_payload(points))  # noqa: SLF001
    assert "横截面" not in ctx
    assert "[" not in ctx.split("\n", 1)[1] if "\n" in ctx else True
    # 按期 desc
    body = [ln for ln in ctx.splitlines() if ln.startswith("- ")]
    assert body and "2026-09-01" in body[0], f"时间序列未按期降序：{body}"


def test_multi_value_without_distinguishing_field_is_still_a_defect():
    """★★★ `fed:policy_range` 那类"同一天多条、**没有字段能区分**"**不许**被
    当成横截面而掩盖过去。

    那是"一个 indicator 多值语义"的缺陷（上限/下限/有效利率挤在一个 id 里，
    取"最新一条"会把区间下限当政策利率报出去 —— 已拆成
    `fed:target_upper` / `fed:target_lower` / `fed:effr`）。
    若渲染层用"同一天 ≥3 条"就判横截面，这个缺陷会**从视野里消失**。
    """
    points = [
        {"indicator": "fed:policy_range", "value": v, "period_date": "2026-09-28",
         "source_name": "FRED", "confidence": 0.95, "extra": {"country": "US"}}
        for v in (4.0, 3.75, 3.88)
    ]
    ctx = _Probe()._build_context(_payload(points))  # noqa: SLF001
    assert "横截面" not in ctx, (
        "把「同一天多条但无区分字段」当成了横截面 —— 这会**掩盖**多值语义缺陷。"
    )


def test_two_same_date_points_are_not_a_cross_section():
    """下限是 3：2 条同日不足以判横截面（避免掩盖上面那类缺陷）。"""
    points = [_member("X:all", 1.0, "甲"), _member("X:all", 2.0, "乙")]
    ctx = _Probe()._build_context(_payload(points))  # noqa: SLF001
    assert "横截面" not in ctx


# ============================================================
# ④ 「没量到」不许变成 0
# ============================================================


def test_missing_value_is_not_rendered_as_zero():
    """★★ 成员的值缺失时渲染 `缺失`，且**排在最后**（不许当 0 参与排序）。"""
    points = [
        _member("ind:sw_third_pe_ttm:all", 30.0, "有值行业"),
        {"indicator": "ind:sw_third_pe_ttm:all", "value": None,
         "period_date": "2026-09-29", "source_name": "AKShare申万行业估值",
         "confidence": 0.95, "extra": {"industry_name": "缺失行业"}},
        _member("ind:sw_third_pe_ttm:all", 10.0, "次低行业"),
    ]
    ctx = _Probe()._build_context(_payload(points))  # noqa: SLF001
    assert "缺失行业" in ctx
    missing_line = next(ln for ln in ctx.splitlines() if "缺失行业" in ln)
    assert "=缺失" in missing_line, f"缺失值未被标注为缺失：{missing_line}"
    assert "=0" not in missing_line, f"缺失值被渲染成 0（假绿）：{missing_line}"
    # 30.0 在 10.0 之前（值降序），缺失值最后
    order = [ln for ln in ctx.splitlines() if ln.startswith("- ")]
    assert "=30.0" in order[0] and "=10.0" in order[1]
    assert "缺失行业" in order[-1]


def test_numeric_strings_are_compared_as_numbers():
    """★ 字符串数字（DB 取回来常是 str）要按**数值**降序，不是字典序。

    字典序下 "9.5" > "10.2"，截断时会把 10.2 挤掉 —— 静默给错子集。
    """
    points = [_member("X:all", "9.5", "甲"), _member("X:all", "10.2", "乙"),
              _member("X:all", "8.1", "丙")]
    ctx = _Probe()._build_context(_payload(points))  # noqa: SLF001
    order = [ln for ln in ctx.splitlines() if ln.startswith("- ")]
    assert "=10.2" in order[0], f"字符串数字未按数值排序：{order}"
