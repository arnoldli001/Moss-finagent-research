"""运行指标端点：LLM调用延迟分位/缓存命中/降级/慢调用。"""

from __future__ import annotations

from fastapi import APIRouter

from src.core.config import get_settings
from src.infrastructure.observability.metrics import read_and_aggregate

router = APIRouter(prefix="/api/v1/metrics", tags=["metrics"])


@router.get("")
async def llm_metrics(limit: int = 1000) -> dict:
    """最近N条LLM调用的聚合指标（默认最近1000条）。"""
    limit = max(1, min(limit, 100_000))
    settings = get_settings()
    metrics = read_and_aggregate(
        f"{settings.llm_audit_dir}/llm_audit.jsonl", limit=limit
    )
    return {"limit": limit, "slow_call_threshold_ms": 3000, "metrics": metrics}


@router.get("/collection_anomalies")
async def collection_anomalies(
    limit: int = 100, since_hours: float = 72.0,
) -> dict:
    """★ 数据采集异常展示区（2026-09-30，用户口径）。

    > 「前端显示"部分节点异常、查询超过 10 秒防撞钟"，这类信息异常信息，
    >  **不要显示在用户界面**。要记录并显示到管理员界面的"运行指标"里，
    >  可以加一块**数据采集异常展示区**，方便我后续维护。」

    * 数据源：`core/collection_anomalies.py`（落登记的 `run_dir`，有界 + 去重）；
    * 鉴权：本路由整体挂在 `metrics` 功能权限下（**只有管理员**看得到，
      见 `domain/platform/config.py` 的 `FEATURES`/`VIEW_FEATURE`）；
    * **口径随数据下发**：`window_hours`/`by_kind`/`bad_lines` 一起返回，
      前端要能显示"这是哪个窗口、多少条、几行读不出来"。
    """
    from src.core.collection_anomalies import KINDS, recent

    limit = max(1, min(limit, 1000))
    since_hours = max(1.0, min(float(since_hours), 24.0 * 30))
    out = recent(limit=limit, since_hours=since_hours)
    out["kinds"] = list(KINDS)
    #: ⚠️ 这张表**必须与 `KINDS` 一一对应**（多一个少一个都会让界面显示成原始
    #: kind 字符串，看起来像"未知类型"）。护栏：
    #: `tests/unit/test_collection_anomalies_kinds.py::test_labels_cover_kinds_exactly`。
    out["kind_labels"] = {
        "gap": "采集缺口（源为空/没有生产者）",
        "timeout": "防撞钟（交互查询超预算未取到）",
        "error": "取数异常（源报错）",
        "not_applicable": "该口径对本主体不适用（不是故障）",
        #: ★ 外部搜索源的运维状态（CHG-0120）：额度/凭据/连通性问题。
        #: **它不是"某个指标取不到"**，而是"找数据源这条路本身出了问题"。
        "search_source": "搜索源异常（额度用尽 / 未配凭据 / 连不上）",
        #: ★ 换源线索（CHG-0120）：**不是故障**，是待人工复核的候选源网址。
        "source_lead": "换源线索（无连接器支持，已联网找到候选源网址·待复核）",
    }
    return out
