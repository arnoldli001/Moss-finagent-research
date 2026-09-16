"""代码安全验证器单测：AST检查、导入白名单、危险调用检测。"""

from __future__ import annotations

import pytest

from src.infrastructure.connectors.code_validator import (
    validate_code,
    validate_connector_code,
)

VALID_CONNECTOR = '''
from __future__ import annotations
import re
from typing import Any
from src.core.exceptions import DataFetchError
from src.core.schemas import DataPoint, DataSourceType, FetchMethod
from src.infrastructure.connectors.base import BaseConnector

class TestConnector(BaseConnector):
    source_name = "test"
    source_url = "https://test.com"

    @staticmethod
    def supports(indicator: str) -> bool:
        return indicator.startswith("test:")

    def get_capabilities(self) -> dict[str, Any]:
        return {"name": "test", "indicators": ["test:x"]}

    async def fetch(self, indicator, start_date=None, end_date=None):
        return []
'''


def test_valid_connector_passes():
    assert validate_connector_code(VALID_CONNECTOR) == []


def test_dangerous_import_blocked():
    bad = VALID_CONNECTOR.replace("import re", "import subprocess")
    issues = validate_code(bad)
    assert any("subprocess" in i for i in issues)


def test_exec_call_blocked():
    bad = VALID_CONNECTOR + "\nx = exec('print(1)')\n"
    issues = validate_code(bad)
    assert any("exec" in i for i in issues)


def test_eval_call_blocked():
    bad = VALID_CONNECTOR + "\nx = eval('1+1')\n"
    issues = validate_code(bad)
    assert any("eval" in i for i in issues)


def test_file_write_blocked():
    bad = VALID_CONNECTOR.replace(
        "return []", "open('/etc/passwd', 'w'); return []"
    )
    issues = validate_code(bad)
    assert any("open" in i or "写入" in i for i in issues)


def test_missing_base_connector_detected():
    bad = "class Foo:\n    pass\n"
    issues = validate_connector_code(bad)
    assert any("BaseConnector" in i for i in issues)


def test_missing_supports_detected():
    bad = VALID_CONNECTOR.replace(
        "@staticmethod\n    def supports", "def _no_supports"
    )
    issues = validate_connector_code(bad)
    assert any("supports" in i for i in issues)


def test_syntax_error_detected():
    bad = "def broken(:\n"
    issues = validate_code(bad)
    assert any("语法" in i or "Syntax" in i for i in issues)


def test_os_system_blocked():
    bad = VALID_CONNECTOR + "\nos.system('rm -rf /')\n"
    issues = validate_code(bad)
    assert any("os.system" in i or "system" in i for i in issues)


@pytest.mark.asyncio
async def test_sandbox_valid_connector():
    from src.infrastructure.connectors.code_validator import sandbox_test

    result = await sandbox_test(VALID_CONNECTOR)
    assert result["ok"] is True
    assert result["class"] == "TestConnector"


@pytest.mark.asyncio
async def test_sandbox_invalid_code():
    from src.infrastructure.connectors.code_validator import sandbox_test

    result = await sandbox_test("class NotAConnector:\n    pass\n")
    assert result["ok"] is False
