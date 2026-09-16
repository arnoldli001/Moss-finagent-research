"""`as_of_panel` 与 `as_of` 的**逐值对拍**（这条测试原本被文档声称存在，实际没有）。

## 为什么必须有它

`PitPanel.as_of_panel` 的文档写着"语义直白版实现（慢但一眼可验证），
`as_of_panel` 走 merge_asof 快路径，**两者结果必须一致（有专门用例对拍）**"——
但项目里从来没有这个用例。后果：

- `as_of_panel` 用 `merge_asof` 时排序默认是**非稳定排序**，同一键并列时取到哪一行
  是未定义的；
- 而同一 `code` 在同一 `usable_date` 上**确实会有多条记录**（不同报告期同日公告，
  或修正公告与原公告同日）；
- 于是两条路径取到不同的记录，`roe` 列实测最大差异 **5942** —— 不报错、不崩溃，
  只是数字不一样。

这个缺陷在一次性能优化里才暴露：把 `fundamental_field` 从逐日 `as_of`
改走 `as_of_panel` 之后，同一次筛选的 `roe` 的 IC 从 0.0386 变成 0.0315。
**换个实现路径结果就变**，等于复现性失效 —— 所以这条对拍是硬要求，不是可选。
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from src.quant.pit import PitPanel


def _records() -> pd.DataFrame:
    """构造含**同日多报告期**与**同日修正公告**的财务记录。"""
    return pd.DataFrame({
        "code": ["000001", "000001", "000001", "000001", "600519", "600519"],
        "name": ["平安银行"] * 4 + ["贵州茅台"] * 2,
        # 000001：20250930 与 20251231 两期都在 20260420 公告（同日并列）
        "report_period": ["20250331", "20250930", "20251231", "20251231",
                          "20250331", "20251231"],
        "ann_date": ["20250425", "20260420", "20260420", "20260420",
                     "20250428", "20260425"],
        # 20251231 的那条在 20260420 有两版（修正公告同日）
        "roe": [10.0, 11.0, 12.0, 13.0, 30.0, 31.0],
        "roa": [0.8, 0.9, 1.0, 1.1, 2.0, 2.1],
    })


DATES = ["20250401", "20250425", "20250426", "20260419", "20260420",
         "20260421", "20260601"]


def test_as_of_panel_matches_as_of_on_large_frame_with_many_ties() -> None:
    """**大样本**对拍：同一 (code, usable_date) 有大量并列时两条路径仍须一致。

    为什么需要这一条（而不是上面那个 6 行的小用例）：
    非稳定排序只在数据量足够大、numpy 切换到真正的快排路径时才会重排并列元素 ——
    实测 6 行的小数据集下，去掉 `kind="stable"` 两条路径**仍然一致**，
    也就是说小用例根本测不出这个缺陷。必须给到几千行、并且人为制造大量并列，
    才能稳定复现（真实数据里 33 万行财务记录就是这个量级）。
    """
    codes = [f"{index:06d}" for index in range(1, 401)]
    # 每个 code 有 12 个报告期；**每两个报告期共享同一个公告日**（制造并列）
    periods = ["20230331", "20230630", "20230930", "20231231",
               "20240331", "20240630", "20240930", "20241231",
               "20250331", "20250630", "20250930", "20251231"]
    rows = []
    for code_index, code in enumerate(codes):
        for period_index, period in enumerate(periods):
            rows.append({
                "code": code, "name": "",
                "report_period": period,
                # 每两期一日公告 → usable_date 大量并列
                "ann_date": f"2026{1 + period_index // 2:02d}15",
                "roe": float(code_index * 100 + period_index),
                "roa": float(period_index),
            })
    panel = PitPanel(pd.DataFrame(rows))
    days = ["20260115", "20260315", "20260515", "20260815", "20261231"]

    fast = panel.as_of_panel(days)
    for day in days:
        slow = panel.as_of(day)
        snapshot = fast.get(day, pd.DataFrame())
        assert len(slow) == len(snapshot), f"{day} 行数不同"
        if len(slow) == 0:
            continue
        common = slow.index.intersection(snapshot.index)
        left = slow.loc[common, "roe"].astype(float)
        right = snapshot.loc[common, "roe"].astype(float)
        bad = ~np.isclose(left, right, atol=1e-9)
        if bad.any():
            codes_bad = list(common[bad])[:3]
            detail = [(code, float(left[code]), float(right[code]))
                      for code in codes_bad]
            pytest.fail(
                f"{day}：{int(bad.sum())}/{len(common)} 个 code 的 roe 不一致，"
                f"例如 {detail}（同日多期公告时取到了不同记录）")


def test_as_of_panel_matches_as_of_on_every_cell() -> None:
    """逐日、逐字段、逐股票对拍：两条路径必须完全一致。"""
    panel = PitPanel(_records())
    fast = panel.as_of_panel(DATES)
    mismatches: list[str] = []
    for day in DATES:
        slow = panel.as_of(day)
        snapshot = fast.get(day, pd.DataFrame())
        if len(slow) != len(snapshot):
            mismatches.append(f"{day}: 行数 {len(slow)} vs {len(snapshot)}")
            continue
        if len(slow) == 0:
            continue
        common = slow.index.intersection(snapshot.index)
        for column in ("roe", "roa"):
            left = slow.loc[common, column].astype(float)
            right = snapshot.loc[common, column].astype(float)
            same_nan = left.isna() == right.isna()
            close = np.isclose(left.fillna(0), right.fillna(0), atol=1e-9)
            bad = (~same_nan | ~close)
            if bad.any():
                codes = list(common[bad])[:3]
                mismatches.append(
                    f"{day}.{column}: {int(bad.sum())} 处不一致，例如 "
                    f"{[(code, float(left[code]), float(right[code])) for code in codes]}")
    assert not mismatches, "as_of_panel 与 as_of 不一致：\n" + "\n".join(mismatches)


def test_same_day_multiple_periods_pick_the_latest_report() -> None:
    """同一公告日有多期报表时，必须取**报告期最新的那一期**。

    这是"当时最新已知"的语义，也是 `as_of` 用 `keep="last"` +
    按 report_period 升序的原始顺序所表达的含义。
    """
    panel = PitPanel(_records())
    snapshot = panel.as_of("20260421")
    assert snapshot.loc["000001", "roe"] == pytest.approx(13.0), \
        "20260420 同日公告了 20250930 与 20251231 两期，应取报告期更晚的 20251231"
    assert panel.as_of_panel(["20260421"])["20260421"].loc["000001", "roe"] \
        == pytest.approx(13.0)


def test_value_invisible_until_usable_date() -> None:
    """公告日 + lag_days 之前取不到该期数据（PIT 的硬承诺）。"""
    panel = PitPanel(_records())
    assert pd.isna(panel.as_of("20250425")["roe"].get("000001")), \
        "公告当天（lag_days=1）还不可见"
    assert panel.as_of("20250426").loc["000001", "roe"] == pytest.approx(10.0)


def test_panel_and_single_agree_on_empty_dates() -> None:
    """没有可见记录时两条路径都应返回空/全 NaN，而不是抛错。"""
    panel = PitPanel(_records())
    early = "20000101"
    assert len(panel.as_of(early)) == 0
    snapshot = panel.as_of_panel([early]).get(early)
    assert snapshot is None or len(snapshot) == 0
