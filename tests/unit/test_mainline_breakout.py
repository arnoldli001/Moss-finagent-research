"""突破触发（越过**自己**的长期震荡上沿）的单测。

## 为什么需要这条触发

用户报障"多个板块的告警滞后真值日、基本在板块顶部才报"。
逐日核对后看到的机制是：**能不能报由绝对阈值 + 加分决定**，
而不是由"这个板块是不是刚突破它自己的震荡区间"决定。

实测（农业种植 885812）：

| 日期 | 六维排名 | total | 等级 |
|---|---:|---:|---|
| 20260617 | 69 | 58.6 | none |
| **20260623** | **8** | 62.3 | **none** ← 板块已启动，却低于中信号线 |
| 20260626 | 45 | 73.5 | strong ← 靠门控加分 +15 才越线 |

绝对阈值是全市场统一的，所以普涨时真领涨的板块会被漏掉；等它"足够极端"
时行情已到中后段。突破触发就是补这一条。

## 这里钉住的不变式

1. `breakout_ceiling` 是**分位数**，且历史不足时返回 `None`（不是 0）；
2. `is_breakout` 必须有**全局下限**：否则长期在 30 分徘徊的板块冲上 35 分
   也算突破（矮子里拔将军），告警会爆；
3. `score_total_history` 必须**严格 PIT**（不含当日）—— 含了就是未来信息。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from src.mainline.scoring import breakout_ceiling, is_breakout  # noqa: E402


def test_ceiling_is_a_quantile_of_own_history() -> None:
    history = [float(x) for x in range(1, 101)]      # 1..100
    # 90 分位（位置法）落在 90 附近
    ceiling = breakout_ceiling(history, quantile=0.90, min_samples=60)
    assert ceiling is not None and 89.0 <= ceiling <= 91.0
    # 分位越高，上沿越高
    low = breakout_ceiling(history, quantile=0.50, min_samples=60)
    high = breakout_ceiling(history, quantile=0.99, min_samples=60)
    assert low is not None and high is not None and low < ceiling < high


def test_ceiling_is_none_when_history_is_short() -> None:
    """历史不足 → `None`（这条路径不生效），**不能**返回 0 ——
    返回 0 会让任何正分都算"突破"。"""
    assert breakout_ceiling([50.0] * 10, quantile=0.90,
                            min_samples=60) is None
    assert breakout_ceiling([], quantile=0.90, min_samples=60) is None


def test_breakout_requires_beating_the_ceiling_and_the_floor() -> None:
    ceiling = 60.0
    # 高于上沿且高于下限 → 突破
    assert is_breakout(65.0, ceiling, min_score=50.0) is True
    # 高于上沿但低于全局下限 → **不算**（矮子里拔将军）：
    # 上沿 40、当日 45 确实越过了自己，但 45 < 下限 50
    assert is_breakout(45.0, 40.0, min_score=50.0) is False
    # 低于上沿 → 不算
    assert is_breakout(59.0, ceiling, min_score=50.0) is False
    # 恰好等于上沿 → 不算（要"越过"）
    assert is_breakout(60.0, ceiling, min_score=50.0) is False
    # 没有历史（None）→ 不算
    assert is_breakout(99.0, None, min_score=50.0) is False
    # 非数值 → 不算
    assert is_breakout(None, ceiling, min_score=50.0) is False


def test_breakout_params_are_coupled_in_config() -> None:
    """`breakout_min_samples` **必须 ≤** `breakout_lookback_days`。

    这是一条真实踩过的坑：实测 `lookback=20/30/40` 配 `min_samples=60` 时
    命中数是 **0** —— 不是效果差，而是"最少 60 个样本"永远满足不了
    "回看 30 天"，`breakout_ceiling` 直接返回 `None`，整条路径静默失效。
    参数耦合不会报错，只会让告警悄无声息地少掉一块，所以必须钉住。
    """
    from src.mainline.config import load_config

    alert = load_config(force=True).alert
    assert alert.breakout_min_samples <= alert.breakout_lookback_days, (
        "min_samples > lookback 会让突破触发永不生效（静默失效）")
    # 历史窗口还要容得下"最近 fresh_days 天都在上沿之下"的判断
    assert alert.breakout_fresh_days < alert.breakout_lookback_days
    assert 0.0 < alert.breakout_quantile < 1.0


def test_long_flat_board_does_not_fire_on_a_tiny_bump() -> None:
    """长期在 30 分徘徊的板块冲上 35 分不能报 —— 这是告警爆量的主因。"""
    history = [28.0, 30.0, 31.0, 29.0] * 20          # 80 天，一直在 30 附近
    ceiling = breakout_ceiling(history, quantile=0.90, min_samples=60)
    assert ceiling is not None and ceiling <= 31.0
    # 35 确实越过了它自己的上沿……
    assert 35.0 > ceiling
    # ……但低于全局下限，所以仍然不报
    assert is_breakout(35.0, ceiling, min_score=50.0) is False


def test_score_total_history_is_strictly_point_in_time(tmp_path: Path) -> None:
    """`score_total_history_sync` 必须**不含当日** —— 含了就是未来信息。

    ⚠️ 这个测试还钉住一个真实踩过的坑：第一版把取历史的方法放在了
    `MainlineDataStore`（它读的是 `mainline_cache.db`，**没有**
    `mainline_score` 表），运行时报 "no such table"，而调用方把异常吞成
    "历史为空" → **突破触发静默失效**。所以必须放在**结果库**仓储上。
    """
    from src.mainline.storage import MainlineRepository

    db = tmp_path / "results.db"
    connection = sqlite3.connect(db)
    connection.execute("CREATE TABLE mainline_score"
                       "(trade_date TEXT, board_code TEXT, total REAL)")
    # 前 6 天写 A 板块，当日（20260107）故意写一个**极大值**：
    # 若实现把当日算进历史，上沿会被它抬起来（分位数变高），测试就会抓到。
    connection.executemany(
        "INSERT INTO mainline_score VALUES(?,?,?)",
        [(f"2026010{i}", "A", 50.0) for i in range(1, 7)]
        + [("20260107", "A", 999.0)])
    connection.commit()
    connection.close()

    repo = MainlineRepository(path=str(db))
    history = repo.score_total_history_sync("20260107", lookback_days=30)
    assert history.get("A") == [50.0] * 6, "当日（999）不能出现在历史里"
    # 当日不在历史 → 上沿仍是 50，999 才算"突破"
    ceiling = breakout_ceiling(history["A"], quantile=0.90, min_samples=5)
    assert ceiling == 50.0
    assert is_breakout(999.0, ceiling, min_score=50.0) is True
