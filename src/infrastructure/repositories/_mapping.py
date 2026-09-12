"""SQLite/PostgreSQL双后端共享的DataPoint行序列化逻辑。"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from src.core.schemas import DataPoint


def point_to_row(p: DataPoint, task_id: str) -> dict[str, Any]:
    """DataPoint → 与fact_data_points列对齐的扁平dict（值均为DB原生类型）。"""
    return {
        "data_id": p.data_id,
        "indicator": p.indicator,
        "value": p.value,
        "unit": p.unit,
        "period_date": p.period_date,
        "extra_json": json.dumps(p.extra, ensure_ascii=False, default=str),
        "source_name": p.source_name,
        "source_url": p.source_url,
        "source_type": p.source_type.value,
        "publish_time": p.publish_time.isoformat() if p.publish_time else None,
        "fetch_time": p.fetch_time.isoformat(),
        "fetch_method": p.fetch_method.value,
        "raw_content_hash": p.raw_content_hash,
        "processed_by": p.processed_by,
        "process_time": p.process_time.isoformat(),
        "confidence": p.confidence,
        "verified": int(p.verified),
        "task_id": task_id,
        "created_at": datetime.now().isoformat(),
    }


def row_to_point(row: dict[str, Any]) -> DataPoint:
    """扁平行（dict-like列名访问）→ DataPoint。"""
    from src.core.schemas import DataSourceType, FetchMethod

    raw = row.get("extra_json") or "{}"
    try:
        extra: Any = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        extra = {}

    def _dt(value: Any) -> datetime | None:
        if not value:
            return None
        if isinstance(value, datetime):
            return value
        try:
            return datetime.fromisoformat(str(value))
        except ValueError:
            return None

    return DataPoint(
        data_id=row["data_id"],
        indicator=row["indicator"],
        value=row.get("value"),
        unit=row.get("unit"),
        period_date=row.get("period_date"),
        extra=extra,
        source_name=row["source_name"],
        source_url=row["source_url"],
        source_type=DataSourceType(row["source_type"]),
        publish_time=_dt(row.get("publish_time")),
        fetch_time=_dt(row.get("fetch_time")) or datetime.now(),
        fetch_method=FetchMethod(row["fetch_method"]),
        raw_content_hash=row["raw_content_hash"],
        processed_by=row["processed_by"],
        process_time=_dt(row.get("process_time")) or datetime.now(),
        confidence=row["confidence"],
        verified=bool(row["verified"]),
    )


# 列顺序（双后端INSERT共用）
COLUMNS = (
    "data_id", "indicator", "value", "unit", "period_date", "extra_json",
    "source_name", "source_url", "source_type", "publish_time", "fetch_time",
    "fetch_method", "raw_content_hash", "processed_by", "process_time",
    "confidence", "verified", "task_id", "created_at",
)
