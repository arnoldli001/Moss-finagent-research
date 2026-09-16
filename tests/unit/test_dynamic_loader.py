"""动态连接器加载器单测。"""

from __future__ import annotations

import pytest

from src.infrastructure.connectors.dynamic_loader import DynamicConnectorLoader

VALID_CONNECTOR = '''
from __future__ import annotations
from typing import Any
from src.infrastructure.connectors.base import BaseConnector

class DynTestConnector(BaseConnector):
    source_name = "dyn_test"
    source_url = "https://test.com"

    @staticmethod
    def supports(indicator: str) -> bool:
        return indicator.startswith("dyn_test:")

    def get_capabilities(self) -> dict[str, Any]:
        return {"name": "dyn_test", "indicators": ["dyn_test:x"]}

    async def fetch(self, indicator, start_date=None, end_date=None):
        from src.core.schemas import DataPoint
        return [DataPoint(indicator=indicator, value=1.0,
                          period_date="2026-09-14",
                          source_name="dyn_test", source_url="https://test.com")]
'''


def test_load_valid_connector(tmp_path):
    conn_file = tmp_path / "dyn_test_connector.py"
    conn_file.write_text(VALID_CONNECTOR, encoding="utf-8")
    loader = DynamicConnectorLoader(tmp_path)
    routes = loader.load_all()
    assert len(routes) == 1
    conn, supports = routes[0]
    assert conn.__class__.__name__ == "DynTestConnector"
    assert supports("dyn_test:abc") is True
    assert supports("other") is False


def test_load_invalid_file_skipped(tmp_path):
    bad_file = tmp_path / "bad_connector.py"
    bad_file.write_text("syntax error (((\n", encoding="utf-8")
    good_file = tmp_path / "good_connector.py"
    good_file.write_text(VALID_CONNECTOR, encoding="utf-8")
    loader = DynamicConnectorLoader(tmp_path)
    routes = loader.load_all()
    assert len(routes) == 1  # bad文件被跳过


def test_reload_clears_cache(tmp_path):
    conn_file = tmp_path / "dyn_test_connector.py"
    conn_file.write_text(VALID_CONNECTOR, encoding="utf-8")
    loader = DynamicConnectorLoader(tmp_path)
    routes1 = loader.load_all()
    assert len(routes1) == 1
    # 删除文件后reload
    conn_file.unlink()
    routes2 = loader.reload()
    assert len(routes2) == 0


def test_list_loaded(tmp_path):
    conn_file = tmp_path / "dyn_test_connector.py"
    conn_file.write_text(VALID_CONNECTOR, encoding="utf-8")
    loader = DynamicConnectorLoader(tmp_path)
    loader.load_all()
    items = loader.list_loaded()
    assert len(items) == 1
    assert items[0]["class"] == "DynTestConnector"


def test_underscore_prefixed_files_skipped(tmp_path):
    skip_file = tmp_path / "_skip_me.py"
    skip_file.write_text(VALID_CONNECTOR, encoding="utf-8")
    loader = DynamicConnectorLoader(tmp_path)
    routes = loader.load_all()
    assert len(routes) == 0


@pytest.mark.asyncio
async def test_loaded_connector_fetchable(tmp_path):
    conn_file = tmp_path / "dyn_test_connector.py"
    conn_file.write_text(VALID_CONNECTOR, encoding="utf-8")
    loader = DynamicConnectorLoader(tmp_path)
    routes = loader.load_all()
    conn, _ = routes[0]
    points = await conn.fetch("dyn_test:abc")
    assert len(points) == 1
    assert points[0].value == 1.0
