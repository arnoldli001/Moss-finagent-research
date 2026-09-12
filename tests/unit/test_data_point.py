"""core.schemas DataPoint契约测试。"""

import pytest
from pydantic import ValidationError

from src.core.schemas import DataPoint, DataSourceType, FetchMethod, hash_content


def test_hash_content_stable_for_dict():
    a = hash_content({"b": 1, "a": "x"})
    b = hash_content({"a": "x", "b": 1})
    assert a == b  # 键序无关
    assert len(a) == 64


def test_data_point_auto_computes_content_hash():
    point = DataPoint(indicator="CPI", value=2.1, extra={"月份": "2026-08"})
    assert len(point.raw_content_hash) == 64


def test_data_point_defaults_follow_contract():
    point = DataPoint(indicator="PPI")
    assert point.data_id.startswith("d_")
    assert point.processed_by == "A01_data_collector"
    assert point.fetch_method == FetchMethod.API_CALL
    assert point.source_type == DataSourceType.API
    assert point.verified is False


def test_data_point_rejects_missing_indicator():
    with pytest.raises(ValidationError):
        DataPoint()  # type: ignore[call-arg]
