"""★★★ FRED 政策利率序列**拆分**回归（2026-09-28 第二十三轮）。

## 这个文件守的那个 bug（比"缺数据"更危险）

`fed:policy_range` 原先把 **三个 FRED 序列写进同一个 indicator**：

    DFEDTARU（目标区间上限）  2026-09-27 = 4.00
    DFEDTARL（目标区间下限）  2026-09-27 = 3.75
    DFF   （有效联邦基金利率） 2026-09-27 = 3.88
    ────────────────────────────────────────────
    三条点的 `indicator` **完全相同**，`period_date` 也相同

于是 **"这个指标的值是多少"这个问题没有答案**：它同时是 4.00、3.75、3.88。

read 侧的实测后果：`_latest_numeric(pts, "fed:policy_range")` 取
`period_date` 最新的**第一条** → 同日三条里挑中**上限或下限之一** →
A08 输出「目标区间 **3.75%**（FRED 口径）」——
**把区间下限当成政策利率报给用户**。数字看着有据，语义是错的。

## 修法的两层

1. **采集侧**（本文件）：三个序列按 `indicator` 分开落库 ——
   `fed:target_upper` / `fed:target_lower` / `fed:effr` 各自是**单值时间序列**；
   旧的 `fed:policy_range` 继续产出（兼容历史库那 3 条点），
   但它的 capabilities 文案明确写了"不要取单点"。
2. **读取侧**（`tests/unit/test_macro_agent.py`）：方向判断优先 `fed:effr`，
   区间必须 `lower` + `upper` **成对**读。
   两层都做才算修好 —— 只做一层，另一层会把 bug 原样带回来。
"""

from __future__ import annotations

import pytest

from src.infrastructure.connectors.fedwatch_connector import (
    _FRED_SERIES,
    FRED_SERIES_BY_RANGE_ID,
    FedWatchConnector,
)

_CSV = "observation_date,DFEDTARU\n2026-09-24,4.00\n2026-09-27,4.00\n"


def test_each_fred_series_has_its_own_indicator_id():
    """★ 三个 FRED 序列必须各有**独立且互不相同**的 indicator id。

    这条是本文件的核心：只要有两个序列共用 id，read 侧取"最新一条"
    就必然在它们之间随机挑一个 —— 而**不会有任何报错**。
    """
    own_ids = [own for _sid, _label, own in _FRED_SERIES]
    assert len(own_ids) == len(set(own_ids)), (
        f"有 FRED 序列共用了 indicator id：{own_ids} —— "
        "read 侧取『最新一条』会挑中任意一条（区间下限被当成利率的那个 bug）"
    )
    for must in ("fed:target_upper", "fed:target_lower", "fed:effr"):
        assert must in own_ids, f"缺少独立序列 {must}"


def test_series_ids_map_to_expected_fred_codes():
    """序列 ID 与 FRED 官方代号必须一一对应（写错就会静默取到别的序列）。"""
    mapping = {own: sid for sid, _label, own in _FRED_SERIES}
    assert mapping["fed:target_upper"] == "DFEDTARU"
    assert mapping["fed:target_lower"] == "DFEDTARL"
    assert mapping["fed:effr"] == "DFF"


def test_legacy_range_id_still_expands_to_all_series():
    """旧混合口径仍要能取到（历史库有 3 条点，且下游能力清单仍引用它）。"""
    subs = FRED_SERIES_BY_RANGE_ID["fed:policy_range"]
    assert set(subs) == {"fed:target_upper", "fed:target_lower", "fed:effr"}


@pytest.mark.parametrize("indicator,expected_series", [
    ("fed:effr", ["DFF"]),
    ("fed:target_upper", ["DFEDTARU"]),
    ("fed:target_lower", ["DFEDTARL"]),
    ("fed:policy_range", ["DFEDTARU", "DFEDTARL", "DFF"]),
])
def test_supports_and_series_selection(indicator, expected_series):
    """★ 独立序列**只取自己那一个**；混合口径取全部三个。

    判据用"实际发起了哪些 FRED 请求"，不看返回值 —— 返回值会被
    mock 掩盖，而"多请求了一个序列"恰恰是拆分没生效的症状。
    """
    assert FedWatchConnector.supports(indicator), f"{indicator} 未被 supports 覆盖"

    asked: list[str] = []

    class _Resp:
        text = _CSV

        @staticmethod
        def raise_for_status() -> None:
            return None

    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, _url, params=None):
            asked.append(str((params or {}).get("id")))
            return _Resp()

    import src.infrastructure.connectors.fedwatch_connector as mod

    orig_client = mod.httpx.AsyncClient
    mod.httpx.AsyncClient = lambda *a, **k: _Client()   # type: ignore[assignment]
    try:
        import asyncio

        points = asyncio.run(FedWatchConnector()._fetch_fred_range(indicator))
    finally:
        mod.httpx.AsyncClient = orig_client  # type: ignore[assignment]

    assert asked == expected_series, (
        f"{indicator} 实际请求了 {asked}，期望 {expected_series}"
    )
    # 关键断言：**返回的每条点都挂同一个（请求的）id** —— 独立序列下
    # 三条点不可能同时出现（那正是"同一 id 多值"的病根）
    assert {p.indicator for p in points} <= {indicator}
    if indicator in ("fed:effr", "fed:target_upper", "fed:target_lower"):
        assert len(points) == 1, (
            f"{indicator} 是**单值序列**，却返回了 {len(points)} 条点："
            f"{[(p.indicator, p.period_date, p.value) for p in points]}"
        )


def test_capabilities_advertise_the_split_series():
    """能力清单必须告诉 A17 用**单值序列**，并显式警告旧口径的坑。

    漏了它 → A17 仍会按 `fed:policy_range` 描述能力，
    甚至把"区间下限"当成利率写进结论（它会照抄上游的错数字）。
    """
    caps = FedWatchConnector().get_capabilities()
    indicators = caps["indicators"]
    for must in ("fed:effr", "fed:target_upper", "fed:target_lower"):
        assert must in indicators, f"能力清单缺 {must}"
    notes = caps["notes"]
    assert "取单点" in notes or "不要" in notes, (
        "能力清单必须显式警告『不要用 fed:policy_range 取单点』"
    )
