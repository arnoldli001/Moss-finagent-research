"""时间列的**两种角色**：期次列 vs 运维列（2026-10-01 裁定，`CHG-0155`）。

## 裁定的证据（真实库实测，不是推演）

`best_for_column` 换成"最新期次优先"之后，`ts_code` 的 rank-0 由
`warehouse.db:quant_adj_factor`（**15,993,661 行**的行情表，`trade_date=20260930`）
变成 `moss_finagent.db:map_stock_concept`（**8,287 行**的映射表，
时间列是 `updated_at=2026-09-30T21:05:43`）。

方向本身修对了（同一天里 `21:05:43` 确实比 `00:00:00` 晚），但它暴露了一个
更根本的问题：**运维列写得最勤，按"最新写入优先"它永远赢** ——
于是系统性地让**小表/缓存表**抢走大表的列。那是"认错"，不是"认新"。

## 裁定

* **期次列**（`trade_date`/`period_date`/`date`/`datetime`/`publish_time`/
  `latest_publish_time`）：描述"数据自己是哪一刻的" ⇒ **参与**期次比较。
* **运维列**（`updated_at`/`created_at`/`fetch_time`/`timestamp`）：描述
  "我们什么时候动过这一行" ⇒ **不参与**期次比较，退回"权威层级 → 行数"。
* **未登记的列**：一律不算期次列（不猜）—— 宁可退回既有顺序。

## 自证

`test_selfproof_operational_column_would_win_without_the_ruling` 把旧行为
（运维列也参与比较）就地重现，断言它**会**让"运维列更新"的那张赢 ——
证明上面那条判据有鉴别力，不是恒绿。
"""
from __future__ import annotations

from src.infrastructure.catalog.column_index import (
    OPERATIONAL_COLUMNS,
    PERIOD_COLUMNS,
    TIME_COLUMN_CANDIDATES,
    is_period_column,
    period_key,
)

# ======================================================================
# 一、分类本身：完备、互斥、且**覆盖**候选清单
# ======================================================================

def test_time_column_roles_partition_the_candidate_list() -> None:
    """★ 两类必须**恰好覆盖**时间列候选清单：不漏（漏了就是"没分类"）、不重。"""
    overlap = PERIOD_COLUMNS & OPERATIONAL_COLUMNS
    assert not overlap, f"两个角色重叠：{sorted(overlap)} —— 一个列不能既是期次又是运维"
    covered = PERIOD_COLUMNS | OPERATIONAL_COLUMNS
    missing = set(TIME_COLUMN_CANDIDATES) - covered
    assert not missing, (
        f"时间列候选里有 {sorted(missing)} 没有归类 —— 新加候选列必须同时定角色"
        "（否则它的行为是「碰巧」而不是「规定」）")
    extra = covered - set(TIME_COLUMN_CANDIDATES)
    assert not extra, (
        f"角色表里有 {sorted(extra)} 不是时间列候选 —— 分类表与候选表必须同源")


def test_unknown_column_is_not_a_period_column() -> None:
    """未登记的列**不算**期次列（不猜）。"""
    assert is_period_column("trade_date") is True
    assert is_period_column("updated_at") is False
    assert is_period_column("some_new_time_col") is False
    assert is_period_column("") is False


# ======================================================================
# 二、行为：运维列不参与期次比较
# ======================================================================

def test_operational_column_does_not_participate_in_period_ranking() -> None:
    """★ 运维列再"新"也不许因此排到别人前面（这正是 ts_code 那次翻车的机制）。"""
    from src.infrastructure.catalog.column_index import _period_component

    fresh_operational = _period_component("2026-09-30T23:59:59", "updated_at")
    older_period = _period_component("20260930", "trade_date")
    # 可比期次那一档永远是第 0 档；运维列落在第 1 档（退回既有顺序）
    assert fresh_operational[0] == 1, "运维列竟然拿到了可比期次的档位"
    assert older_period[0] == 0, "期次列应当拿到可比档位"
    assert older_period < fresh_operational, (
        "排序键方向反了：期次列应当排在运维列之前")


def test_period_column_with_time_of_day_still_ranks_fresher() -> None:
    """方向修正在**期次列**上仍然成立（同一天里带时分秒的更新）。"""
    from src.infrastructure.catalog.column_index import _period_component

    with_time = _period_component("2026-09-30T21:05:43", "publish_time")
    date_only = _period_component("20260930", "trade_date")
    assert with_time[0] == 0 and date_only[0] == 0
    assert period_key("2026-09-30T21:05:43") > period_key("20260930")   # type: ignore[operator]
    assert with_time < date_only, "同一天里带时分秒的期次应当更靠前"


def test_selfproof_operational_column_would_win_without_the_ruling() -> None:
    """★★ 自证：把运维列当成期次列（旧行为）⇒ 它**会**赢。

    没有这一条，"运维列不参与"这条判据可能只是因为两边恰好相等而恒绿。
    """
    from src.infrastructure.catalog.column_index import _period_component

    def old_behaviour(latest: str) -> tuple[int, tuple[int, ...], tuple[int, str]]:
        """旧行为：不看列的角色，任何能解析的时间都参与期次比较。"""
        key = period_key(latest)
        if key is not None:
            return (0, tuple(-x for x in key), (0, ""))
        return (1, (), (0, ""))

    old_fresh = old_behaviour("2026-09-30T21:05:43")
    old_older = old_behaviour("20260930")
    assert old_fresh < old_older, (
        "旧行为下运维列竟然没赢 ⇒ 这条自证证明不了裁定的必要性")
    # 而裁定后的行为必须相反：运维列落在第 1 档，期次列在第 0 档
    assert _period_component("2026-09-30T21:05:43", "updated_at") > \
        _period_component("20260930", "trade_date")
