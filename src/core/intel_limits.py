"""平台内部数据的**共享上限**（生产者与消费者必须同源的那几个数）。

## 为什么单独立一个模块

这些数字的特点是：**一方按它截断/限时，另一方按它决定"结论能说多强"**。
写在两处必然漂移，而漂移的症状不是报错，是**结论失真或无限等待**：

- `UNLOCK_DETAIL_TOP_N`：`calendar_store` 按它截断每日解禁明细；
  `platform_data_connector` 按它决定"回退路径的空结果"能不能当"无解禁"（**不能**）。
- `QUERY_DEADLINE_SEC`：`ConnectorRouter` 的交互路径预算 +
  `supervisor._collect_one` 的硬上限（两侧同源）。

放在 `src/core/` 是因为两侧都能 import 它（domain / infrastructure 都依赖 core），
不会造出"领域层反向依赖连接器"这种别扭的导入方向。
"""

from __future__ import annotations

#: 投资日历 JSON 明细里**每天保留的个股条数**（按解禁市值降序取前 N）。
#: ⚠️ 改大改小都要同时想清楚：消费者会据此决定"回退路径的空结果"能不能被
#: 当成"无解禁"（现在**不能**，因为它是截断过的）。真正完整的逐股数据在
#: `app_db::unlock_plan`（月频作业 `unlock_plan_monthly` 落库）。
UNLOCK_DETAIL_TOP_N = 10


# ======================================================================
# 查询防撞钟（用户 2026-09-29 口径：**超过 10 秒没找到大概率就是找不到**）
# ======================================================================
#
# ## 为什么要有它（本轮实测的现场）
#
# 一次真实端到端里**墙钟 275s，其中 254s 花在采集**，而 254s 里有 **251.4s
# 是单一指标**（`大股东质押比例` 的"质押股东明细整表聚合"：126,826 行 /
# 254 个分页请求）。用户看到的是"分析跑了 4 分半"，而它其实只在等一条指标。
#
# ## 定 10 秒的**实测依据**（本机，2026-09-29）
#
# | 指标族 | 实测耗时 | 10s 是否切掉 |
# |---|---|---|
# | 平台族（估值水位/概念拥挤度/行业拥挤度/行业轮动/解禁计划） | 9~62 ms | 不切 |
# | 板块资金流（冷，含一次网络截面） | **6.7 s** | 不切（最慢的正常族） |
# | 申万/指数截面类 | 1~5 s | 不切 |
# | 双创个股截面 `mkt:cybkcb:spot_summary` | 13.4 s（实测，有 5min TTL + 定时预热） | 切 ⇒ 靠预热 |
# | CME FedWatch（本机不可达） | 23.4 s 超时 | 切（本来就拿不到） |
# | 质押股东明细整表 | **251.4 s** | 切（重活，必须挪去定时作业） |
#
# 也就是说：**能用的数据在 7 秒内都能拿到**，10 秒这条线只切掉
# "不可达"与"病态慢"两类 —— 正是用户说的"超过 10 秒大概率就是找不到"。
#
# ## 它管哪些路径（**只管交互路径**）
#
# - 管：A01 采集（`supervisor._collect_one`）、A17 的 `query_data` 工具；
# - **不管**：定时作业/预热路径（`catalog_collection` 等）—— 重活正是要在那里做，
#   掐掉它们会让"预热养缓存"永远养不起来（本项目实测过这类"修一个坏一个"）。
#
# 覆盖方式：`MOSS_QUERY_DEADLINE_SEC`（.env 可写；改它就是改首屏体验，请同步改本节表格）。
QUERY_DEADLINE_SEC: float = 10.0


def query_deadline_sec() -> float:
    """生效的防撞钟秒数。

    读取顺序：`os.environ` → **Settings**（`.env` 走这一路）→ 模块默认。
    ⚠️ 为什么要有 Settings 这一路（2026-09-29 实测陷阱）：pydantic-settings
    只把 `.env` 读进 Settings 对象、**不写回 `os.environ`** ——
    只读 `os.environ` 的话，`.env` 里配的值**永远不生效**，而症状是
    "改了个寂寞"（默认值恰好等于期望值时甚至看不出来）。
    """
    import os

    raw = os.environ.get("MOSS_QUERY_DEADLINE_SEC", "") or ""
    if not raw:
        try:
            from src.core.config import get_settings

            raw = str(getattr(get_settings(), "query_deadline_sec", "") or "")
        except Exception:  # noqa: BLE001 配置读不到 → 用默认值（不阻断）
            raw = ""
    if not raw:
        return QUERY_DEADLINE_SEC
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return QUERY_DEADLINE_SEC
    return value if value > 0 else QUERY_DEADLINE_SEC


def deadline_reason(indicator: str, seconds: float) -> str:
    """防撞钟终止时的**统一话术**（人话 + 数字 + 出路）。"""
    return (f"查询超过 {seconds:.0f} 秒防撞钟，自动终止 {indicator}（"
            "交互路径不为单条指标无限等待；重活应由定时作业预热，"
            "或调 MOSS_QUERY_DEADLINE_SEC 放宽）")


__all__ = [
    "QUERY_DEADLINE_SEC",
    "UNLOCK_DETAIL_TOP_N",
    "deadline_reason",
    "query_deadline_sec",
]
